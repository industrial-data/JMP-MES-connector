# -*- coding: utf-8 -*-
"""
PI Asset Framework (AF) access — the core of v4.0.

Seeq-style asset-centric workflow: users search ATTRIBUTES (e.g. "Temperature")
across the AF element hierarchy, browse the matches in a collapsible
parent/child tree, and extract "per asset": one column per attribute NAME plus
an `Asset` column, timestamps repeated per asset (long/stacked format — like
JMP's Tables > Concatenate, which stacks rows of same-named columns). This
avoids one column per asset-attribute pair (Temperature Reactor A/B/C…) and
lets users swap the analyzed asset with a simple row filter (local data
filter) instead of rebuilding their analysis.

PI Web API endpoints used (all GET):
- /assetservers?name=<af_server>                    -> AF server WebId
- /assetservers/{webid}/assetdatabases              -> databases
- /assetdatabases/{webid}/elementattributes         -> attribute search across
      the full element hierarchy (searchFullHierarchy=true). This is the
      server-side search behind the GUI search bar in AF mode.
- /attributes?path=\\\\AF\\DB\\Element|Attribute    -> attribute WebId + Type
- /streams/{webid}/...                              -> data (same streams
      controller as DA points — attribute WebIds are streamable), reused from
      pi_webapi.py.
- /assetdatabases/{webid}/eventframes               -> event-frame search
      (names + templates), used by the GUI's Event frames filter.

Attribute identity: the full AF path (`\\\\AFSRV\\DB\\Plant\\Unit\\Reactor A|Temperature`)
— unique across assets, unlike the bare attribute name.
"""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import numpy as np
import pandas as pd

from . import pi_webapi as _pi
from .auth import check_response, get_session

PAGING_DELAY_S = 0.02


# ---------------------------------------------------------------------------
# AF server / databases
# ---------------------------------------------------------------------------
def _af_server_webid(base: str, af_server: str) -> str:
    s = get_session(base)
    r = s.get(f"{base}/assetservers", params={"name": af_server}, timeout=60)
    _pi._log_url(r)
    r = check_response( r )
    body = r.json()
    if "WebId" in body:
        return body["WebId"]
    items = body.get("Items", [])
    if items:
        return items[0]["WebId"]
    raise RuntimeError(f"PI AF server '{af_server}' not found at {base}")


def _databases(base: str, af_webid: str) -> list[dict]:
    s = get_session(base)
    r = s.get(f"{base}/assetservers/{af_webid}/assetdatabases",
              params={"selectedFields": "Items.WebId;Items.Name;Items.Path"}, timeout=60)
    _pi._log_url(r)
    r = check_response( r )
    return r.json().get("Items", [])


# ---------------------------------------------------------------------------
# Attribute search (the AF-mode search bar)
# ---------------------------------------------------------------------------
def search_attributes(base_url: str, af_server: str, name_filter: str = "",
                      description_filter: str = "",
                      max_results: int | None = None) -> pd.DataFrame:
    """Search attributes across every database of the AF server.

    Returns the GUI contract columns: tagnames / descriptions / units / type /
    path — where `tagnames` is the FULL attribute path (unique identity used
    for extraction), `path` the element path, and the short attribute name is
    derivable from the path tail. Description filtering is applied locally
    (elementattributes has no description filter parameter).
    """
    base = base_url.rstrip("/")
    af_webid = _af_server_webid(base, af_server)
    s = get_session(base)

    nf = (name_filter or "").strip()
    if nf and "*" not in nf and "?" not in nf:
        nf = f"*{nf}*"

    rows: list[dict] = []
    for db in _databases(base, af_webid):
        start_index, page = 0, 1000
        while max_results is None or len(rows) < max_results:
            count = page if max_results is None else min(page, max_results - len(rows))
            params = {
                "searchFullHierarchy": "true",
                "startIndex": start_index,
                "maxCount": count,
                "selectedFields": "Items.WebId;Items.Name;Items.Path;Items.Description;"
                                  "Items.DefaultUnitsName;Items.Type",
            }
            if nf:
                params["attributeNameFilter"] = nf
            r = s.get(f"{base}/assetdatabases/{db['WebId']}/elementattributes",
                      params=params, timeout=120)
            _pi._log_url(r)
            r = check_response( r )
            items = r.json().get("Items", [])
            if not items:
                break
            rows.extend(items)
            start_index += len(items)
            if len(items) < count:
                break
            time.sleep(PAGING_DELAY_S)

    if not rows:
        return pd.DataFrame(columns=["tagnames", "descriptions", "units", "type", "path"])

    df = pd.DataFrame(rows)
    for src, dst in [("Path", "tagnames"), ("Description", "descriptions"),
                     ("DefaultUnitsName", "units"), ("Type", "type")]:
        df[dst] = df.get(src, "")
        df[dst] = df[dst].fillna("")
    # element path (everything before the |attribute part)
    df["path"] = df["tagnames"].str.split("|").str[0]

    d = (description_filter or "").strip().strip("*")
    if d:
        df = df[df["descriptions"].str.contains(d, case=False, na=False, regex=False)]

    df = df.drop_duplicates(subset="tagnames")
    return df[["tagnames", "descriptions", "units", "type", "path"]].reset_index(drop=True)


def attribute_type(base_url: str, attribute_path: str) -> str:
    """Value type of one attribute (by full AF path)."""
    base = base_url.rstrip("/")
    s = get_session(base)
    r = s.get(f"{base}/attributes",
              params={"path": attribute_path, "selectedFields": "Type"}, timeout=60)
    _pi._log_url(r)
    r = check_response( r )
    return str(r.json().get("Type", ""))


# ---------------------------------------------------------------------------
# Event frames (Seeq-style "conditions")
# ---------------------------------------------------------------------------
def search_event_frames(base_url: str, af_server: str, name_filter: str = "",
                        template_filter: str = "", start: str = "*-30d",
                        end: str = "*", max_results: int = 2000) -> pd.DataFrame:
    """Event frames overlapping [start, end] across all databases.

    Returns: Name / Template / Start / End / Path — the GUI lists them so the
    user can restrict the extraction to the selected frames' time windows.
    """
    base = base_url.rstrip("/")
    af_webid = _af_server_webid(base, af_server)
    s = get_session(base)

    nf = (name_filter or "").strip()
    if nf and "*" not in nf and "?" not in nf:
        nf = f"*{nf}*"

    rows: list[dict] = []
    for db in _databases(base, af_webid):
        params = {
            "searchMode": "Overlapped",
            "startTime": start,
            "endTime": end,
            "maxCount": max_results,
            "selectedFields": "Items.Name;Items.TemplateName;Items.StartTime;"
                              "Items.EndTime;Items.Path",
        }
        if nf:
            params["nameFilter"] = nf
        if (template_filter or "").strip():
            params["templateName"] = template_filter.strip()
        r = s.get(f"{base}/assetdatabases/{db['WebId']}/eventframes",
                  params=params, timeout=120)
        _pi._log_url(r)
        r = check_response( r )
        rows.extend(r.json().get("Items", []))

    if not rows:
        return pd.DataFrame(columns=["Name", "Template", "Start", "End", "Path"])
    df = pd.DataFrame(rows).rename(columns={
        "TemplateName": "Template", "StartTime": "Start", "EndTime": "End"})
    for c in ("Name", "Template", "Start", "End", "Path"):
        if c not in df.columns:
            df[c] = ""
    return df[["Name", "Template", "Start", "End", "Path"]].reset_index(drop=True)


def apply_event_frames(wide: pd.DataFrame, ef_list: list[dict]) -> pd.DataFrame:
    """Keep only rows inside any selected event frame; label them.

    ef_list items: {"name": ..., "start": iso-utc, "end": iso-utc}.
    Adds an `EventFrame` column with the (first) matching frame name.
    """
    if not ef_list or wide.empty or "TS_UTC" not in wide.columns:
        return wide
    ts = pd.to_datetime(wide["TS_UTC"])
    labels = pd.Series([None] * len(wide), index=wide.index, dtype=object)
    keep = pd.Series(False, index=wide.index)
    for ef in ef_list:
        lo = pd.to_datetime(ef["start"], utc=True).tz_localize(None)
        hi = pd.to_datetime(ef["end"], utc=True).tz_localize(None) if ef.get("end") else ts.max()
        m = (ts >= lo) & (ts <= hi)
        labels[m & labels.isna()] = ef.get("name", "")
        keep |= m
    out = wide[keep].copy()
    out.insert(2, "EventFrame", labels[keep].values)
    return out.reset_index(drop=True)


# ---------------------------------------------------------------------------
# Asset-stacked extraction (the v4.0 headline feature)
# ---------------------------------------------------------------------------
def extract_assets(base_url: str, attribute_paths: list[str], method: str,
                   start: str, end: str, interval_s: int,
                   filter_expression: str = "") -> pd.DataFrame:
    """Extract AF attributes stacked per asset (long format).

    Output: TS, TS_UTC, Asset, <one column per attribute NAME>.
    - Attributes are grouped by their parent element (the asset); each asset's
      attributes are extracted on the same aligned grid and become one block
      of rows with the asset name in `Asset`. Blocks are concatenated
      vertically (JMP Tables > Concatenate semantics): with 3 reactors the
      timestamps appear 3 times, once per reactor.
    - The columns are the UNION of attribute names across assets; an asset
      missing an attribute simply gets missing values in that column.
    """
    base = base_url.rstrip("/")

    # asset (element path) -> {attribute name -> full path}
    assets: dict[str, dict[str, str]] = {}
    for p in attribute_paths:
        elem, _, attr = p.rpartition("|")
        assets.setdefault(elem, {})[attr] = p

    # stable, deterministic column order: first appearance across the selection
    all_attrs: list[str] = []
    for attrs in assets.values():
        for a in attrs:
            if a not in all_attrs:
                all_attrs.append(a)

    def _one_asset(elem: str, attrs: dict[str, str]) -> pd.DataFrame:
        paths = list(attrs.values())
        labels = list(attrs.keys())
        wide = _pi.extract(base, "", paths, labels, method, start, end,
                           int(interval_s), filter_expression)
        asset_name = elem.rpartition("\\")[2] or elem
        wide.insert(2, "Asset", asset_name)
        # union of columns: add the attributes this asset does not have
        for a in all_attrs:
            if a not in wide.columns:
                wide[a] = np.nan
        return wide[["TS", "TS_UTC", "Asset"] + all_attrs]

    parts = []
    for elem in assets:  # assets sequentially; tags within an asset in parallel
        parts.append(_one_asset(elem, assets[elem]))

    out = pd.concat(parts, axis=0, ignore_index=True)
    return out.sort_values(["Asset", "TS_UTC"], kind="stable").reset_index(drop=True)
