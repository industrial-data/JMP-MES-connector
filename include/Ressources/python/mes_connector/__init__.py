# -*- coding: utf-8 -*-
"""
mes_connector — REST extraction layer for the JMP MES Data Retrieval add-in.

Called from JSL (JMP 19 embedded Python) via `include/Ressources_REST.jsl`.
See README.md in this folder for the architecture and the JSL/Python contracts.

Public API (everything JSL calls lives here):
    set_credentials(host, user, password)   # from the JSL login dialog
    test_connection(server_type, base_url, da_server)
    search_tags(server_type, base_url, da_server, name, description, af_server="")
    pi_extract(base_url, da_server, tags, labels, method, start, end,
               interval_s, filters_json)                     # one-shot
    pi_extract_begin / pi_extract_chunk / pi_extract_end     # chunked, for the
                                                             # JSL progress bar
    pi_distinct_values(base_url, da_server, tag, start, end, interval_s)
    ip21_sql(base_url, datasource, sql)

Conventions:
- server_type is "PI" or "IP21" (same strings the JSL GUI uses).
- All functions return pandas DataFrames (JMP converts them to data tables)
  or raise; auth.AuthRequired is caught by JSL to show a login dialog.
- Every HTTP request URL is print()ed so it lands in the JMP log for
  debugging (same philosophy as v2.x echoing the PowerShell commands).

URL tolerance: users may type WebAPI addresses with or without the scheme and
with or without a trailing '/'; `normalize_base_url` fixes them up:
    "piserver.corp.com/piwebapi/"  -> "https://piserver.corp.com/piwebapi"
    "piserver.corp.com"            -> "https://piserver.corp.com/piwebapi"
    "ip21host.corp.com"            -> "http://ip21host.corp.com/ProcessData/AtProcessDataREST.dll"

PLANNED (v3.x, groundwork only): PI AF attribute search. When the server list
provides a PI_AF_Server, a future search mode will browse AF elements
(`/assetservers` -> `/assetdatabases` -> `/elements` -> `/attributes`) and the
GUI will render a collapsible element hierarchy (JSL Tree Box):
    ▾ Parent Group
        Child item 1
        ▾ Nested Child Group
            Grandchild item
Search results will then be labeled  tagname (description) [units] {path}
(the AF path replacing the {type} suffix used for plain tag search).
The af_server parameter is already plumbed through search_tags for this.
"""
from __future__ import annotations

import pandas as pd

from . import filters as _filters
from . import ip21_rest as _ip21
from . import pi_webapi as _pi
from .auth import (  # noqa: F401 (re-exported for JSL)
    AuthRequired,
    clear_credentials,
    configure_tls,
    forget_credentials,
    set_credentials,
)

__version__ = "3.0.0"


def normalize_base_url(server_type: str, url: str) -> str:
    """Make user-typed WebAPI addresses canonical (see module docstring)."""
    u = (url or "").strip().rstrip("/")
    if not u:
        return u
    if "://" not in u:
        # No scheme typed: PI Web API defaults to HTTPS, Aspen's REST dll to
        # plain HTTP. Some servers only answer on the other one — in that case
        # type the scheme explicitly in the WebAPI URL (http://... or
        # https://...): a user-provided scheme is ALWAYS kept as-is.
        u = ("https://" if server_type == "PI" else "http://") + u
    low = u.lower()
    if server_type == "PI" and "piwebapi" not in low:
        u += "/piwebapi"
    if server_type == "IP21" and "atprocessdatarest" not in low:
        u += "/ProcessData/AtProcessDataREST.dll"
    return u


def test_connection(server_type: str, base_url: str, da_server: str) -> str:
    """Cheap round-trip to verify the REST endpoint answers ("OK" or raises)."""
    base = normalize_base_url(server_type, base_url)
    if server_type == "PI":
        _pi._dataserver_webid(base, da_server)
    else:
        _ip21.test_connection_sql(base, da_server)
    return "OK"  # JSL checks for this literal


def search_tags(server_type: str, base_url: str, da_server: str,
                name: str = "", description: str = "",
                af_server: str = "") -> pd.DataFrame:
    """Tag search; columns: tagnames, descriptions, units, type, path.

    The first four columns are the JSL GUI contract (same as legacy SQL).
    `path` is extra: filled for PI (\\\\server\\tag), empty for IP21 — reserved
    for the planned AF attribute search labels (see module docstring).
    `af_server` is accepted but not used yet (future AF element browse).
    """
    base = normalize_base_url(server_type, base_url)
    if server_type == "PI":
        return _pi.search_tags(base, da_server, name, description)
    # IP21: the JSL builds the search SQL itself (legacy query, unchanged) and
    # calls ip21_sql() directly, so this branch is only a convenience for tests.
    sql = (
        "SELECT name as tagnames WIDTH 80,"
        " name->ip_description as descriptions,"
        " name->ip_eng_units as units,"
        " name->ip_tag_type as type"
        " FROM all_records"
        f" WHERE tagnames like '%{name}%' AND descriptions like '%{description}%';"
    )
    df = _ip21.ip21_sql(base, da_server, sql)
    if "path" not in df.columns:
        df["path"] = ""
    return df


def ip21_sql(base_url: str, datasource: str, sql: str) -> pd.DataFrame:
    """Run a SQLplus query through Aspen's REST wrapper (URL normalized)."""
    return _ip21.ip21_sql(normalize_base_url("IP21", base_url), datasource, sql)


# ---------------------------------------------------------------------------
# PI extraction — one-shot and chunked variants
# ---------------------------------------------------------------------------
def _prepare_filters(filters_json: str):
    """-> (filters, condition, filter_expression). Simple comparisons are
    pushed server-side as one shared PI filterExpression; Like/Not Like/In
    are applied locally in pandas after extraction (see filters.py)."""
    flt, condition = _filters.parse_filters_json(filters_json)
    fe = ""
    if flt and _filters.can_push_server_side(flt):
        fe = _filters.build_filter_expression(flt, condition)
    return flt, condition, fe


def pi_extract(base_url: str, da_server: str, tags: list[str], labels: list[str],
               method: str, start: str, end: str, interval_s: int,
               filters_json: str = "") -> pd.DataFrame:
    """One-shot PI extraction with the add-in's registered filters applied."""
    base = normalize_base_url("PI", base_url)
    flt, condition, fe = _prepare_filters(filters_json)
    wide = _pi.extract(base, da_server, tags, labels, method, start, end, interval_s, fe)
    if flt and not fe:
        wide = _filters.apply_local_filters(wide, flt, condition)
    return wide


# Chunked variant: JSL drives a loop of small chunks so it can update its
# progress bar between chunks (Python can't call back into a running JSL loop).
_extract_session: dict = {}


def pi_extract_begin(base_url: str, da_server: str, method: str, start: str,
                     end: str, interval_s: int, filters_json: str = "") -> str:
    flt, condition, fe = _prepare_filters(filters_json)
    _extract_session.clear()
    _extract_session.update(
        base=normalize_base_url("PI", base_url), da=da_server, method=method,
        start=start, end=end, interval=int(interval_s),
        flt=flt, condition=condition, fe=fe, parts=[],
    )
    return "OK"


def pi_extract_chunk(tags: list[str], labels: list[str]) -> str:
    s = _extract_session
    wide = _pi.extract(s["base"], s["da"], list(tags), list(labels),
                       s["method"], s["start"], s["end"], s["interval"], s["fe"])
    s["parts"].append(wide.set_index("TS_UTC"))
    return "OK"


def pi_extract_end() -> pd.DataFrame:
    """Merge all chunks on TS_UTC, rebuild TS, apply local filters if needed."""
    s = _extract_session
    parts = [p.drop(columns=["TS"], errors="ignore") for p in s["parts"]]
    wide = pd.concat(parts, axis=1, join="outer").sort_index()
    out = wide.reset_index()
    ts_utc = pd.to_datetime(out["TS_UTC"])
    from datetime import datetime
    local_tz = datetime.now().astimezone().tzinfo
    out.insert(0, "TS", ts_utc.dt.tz_localize("UTC").dt.tz_convert(local_tz).dt.tz_localize(None))
    if s["flt"] and not s["fe"]:
        out = _filters.apply_local_filters(out, s["flt"], s["condition"])
    _extract_session.clear()
    return out


def pi_distinct_values(base_url: str, da_server: str, tag: str,
                       start: str, end: str, interval_s: int = 60) -> pd.DataFrame:
    """Distinct values of one tag over a window (GUI 'Preview' button).

    Returns columns NAME / VALUE like the legacy SQL DISTINCT preview.
    """
    base = normalize_base_url("PI", base_url)
    wide = _pi.extract(base, da_server, [tag], ["VALUE"], "Interpolated",
                       start, end, int(interval_s))
    vals = wide["VALUE"].dropna().unique() if "VALUE" in wide.columns else []
    return pd.DataFrame({"NAME": [tag] * len(vals), "VALUE": [str(v) for v in vals]})
