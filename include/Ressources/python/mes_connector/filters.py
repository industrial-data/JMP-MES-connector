# -*- coding: utf-8 -*-
"""
Filter handling for the PI Web API path.

The JSL GUI lets users register filters like `TAG_A > 5`, nest two filters
(`(TAG_A > 5) AND (TAG_B = "on")`), and choose a global condition
(ALL = AND / ANY = OR) across registered filters. In v2.x these were compiled
to SQL (INNER JOIN / UNION on timestamps). For PI Web API we reproduce the
same semantics in two stages:

1. SERVER-SIDE (preferred): if *every* comparison is a plain one
   (=, <, >, <=, >=, Not Equal), we build one PI `filterExpression` string in
   Performance-Equation syntax, e.g.  ('TAG_A' > 5) and ('TAG_B' = "on").
   PE expressions may reference any tag by name, so one shared expression is
   applied to every stream request — all tags keep aligned timestamps, exactly
   like the SQL INNER JOIN did.
2. LOCAL (fallback): `Like`, `Not Like` and `In` have no direct PE equivalent,
   so when any filter uses them we skip filterExpression and instead apply a
   pandas mask on the extracted wide table (`apply_local_filters`).

JSL serializes Filters_AA to JSON before calling Python. Expected structure —
a list of filter dicts:
    {"tags":   ["TAG_A"]            or ["TAG_A", "TAG_B"]   (nested pair),
     "comps":  [">"]                or [">", "="],
     "values": ["5"]                or ["5", "'on'"]}
plus a global `condition`: "AND" | "OR".

NOTE on nesting semantics (inherited from v2.x, see f_SQL_CREATE_ONE_FILTER):
the two halves of a *nested* filter are combined with the OPPOSITE of the
global condition (nesting exists precisely to mix AND and OR).
"""
from __future__ import annotations

import json
import re

import numpy as np
import pandas as pd

SIMPLE_COMPS = {"=", ">", "<", ">=", "<=", "Not Equal"}


def parse_filters_json(filters_json: str) -> tuple[list[dict], str]:
    """Decode the JSON produced by JSL's f_Filters_ToJSON."""
    if not filters_json or not filters_json.strip():
        return [], "AND"
    payload = json.loads(filters_json)
    return payload.get("filters", []), payload.get("condition", "AND") or "AND"


def _strip_quotes(v: str) -> str:
    v = str(v).strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
        return v[1:-1]
    return v


def _is_number(v: str) -> bool:
    try:
        float(_strip_quotes(v))
        return True
    except (TypeError, ValueError):
        return False


# ---------------------------------------------------------------------------
# 1) Server-side: PI filterExpression (Performance Equation syntax)
# ---------------------------------------------------------------------------
def can_push_server_side(filters: list[dict]) -> bool:
    """True when every comparison has a PE equivalent."""
    return all(c in SIMPLE_COMPS for f in filters for c in f.get("comps", []))


def _pe_one(tag: str, comp: str, value: str) -> str:
    op = "<>" if comp == "Not Equal" else comp
    v = _strip_quotes(value)
    rhs = v if _is_number(value) else f'"{v}"'
    return f"('{tag}' {op} {rhs})"


def build_filter_expression(filters: list[dict], condition: str) -> str:
    """One PE expression combining all registered filters.

    Applied identically to every stream request so all extracted tags stay on
    the same (filtered) timestamp grid.
    """
    if not filters:
        return ""
    outer = " and " if condition == "AND" else " or "
    inner = " or " if condition == "AND" else " and "  # opposite, see module doc
    parts = []
    for f in filters:
        tags, comps, values = f["tags"], f["comps"], f["values"]
        pieces = [
            _pe_one(tags[min(i, len(tags) - 1)], comps[i], values[i])
            for i in range(len(comps))
        ]
        parts.append("(" + inner.join(pieces) + ")" if len(pieces) > 1 else pieces[0])
    return outer.join(parts)


# ---------------------------------------------------------------------------
# 2) Local fallback: pandas mask on the wide extracted table
# ---------------------------------------------------------------------------
def _find_column(wide: pd.DataFrame, tag: str) -> str | None:
    """Column whose label starts with the bare tag name (labels are
    'TAG (description) [unit] {type}')."""
    for col in wide.columns:
        if col in ("TS", "TS_UTC"):
            continue
        if col == tag or str(col).startswith(tag + " ") or str(col).startswith(tag + "("):
            return col
    return None


def _mask_one(series: pd.Series, comp: str, value: str) -> pd.Series:
    v = _strip_quotes(value)
    if comp in (">", "<", ">=", "<=") or (comp in ("=", "Not Equal") and _is_number(value)):
        s = pd.to_numeric(series, errors="coerce")
        n = float(v)
        return {
            ">": s > n, "<": s < n, ">=": s >= n, "<=": s <= n,
            "=": s == n, "Not Equal": s != n,
        }[comp]
    s = series.astype(str)
    if comp == "=":
        return s == v
    if comp == "Not Equal":
        return s != v
    if comp in ("Like", "Not Like"):
        m = s.str.contains(re.escape(v), case=False, na=False)
        return m if comp == "Like" else ~m
    if comp == "In":
        items = [_strip_quotes(x) for x in str(value).split(",")]
        return s.isin(items)
    raise ValueError(f"Unsupported comparator: {comp!r}")


def apply_local_filters(wide: pd.DataFrame, filters: list[dict], condition: str) -> pd.DataFrame:
    """Keep only rows (timestamps) satisfying the registered filters."""
    if not filters or wide.empty:
        return wide
    outer_and = condition == "AND"
    total = None
    for f in filters:
        tags, comps, values = f["tags"], f["comps"], f["values"]
        mask = None
        inner_and = not outer_and  # opposite of global condition, see module doc
        for i in range(len(comps)):
            tag = tags[min(i, len(tags) - 1)]
            col = _find_column(wide, tag)
            if col is None:
                # Filter tag was not extracted -> cannot evaluate; skip this piece
                continue
            m = _mask_one(wide[col], comps[i], values[i]).fillna(False)
            mask = m if mask is None else ((mask & m) if inner_and else (mask | m))
        if mask is None:
            continue
        total = mask if total is None else ((total & mask) if outer_and else (total | mask))
    return wide if total is None else wide[total.values].reset_index(drop=True)
