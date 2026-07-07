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


def _databases(base: str, af_webid: str, database: str = "") -> list[dict]:
    """Databases of an AF server; restricted to one when `database` is given.

    v4.1: an AF server can host MANY databases — searching all of them is slow
    and returns attributes the user does not care about, so the server list's
    AF_Database field narrows the scope. A named database that does not exist
    is an ERROR (typo in the server list), not an empty result.
    """
    s = get_session(base)
    r = s.get(f"{base}/assetservers/{af_webid}/assetdatabases",
              params={"selectedFields": "Items.WebId;Items.Name;Items.Path"}, timeout=60)
    _pi._log_url(r)
    r = check_response( r )
    items = r.json().get("Items", [])
    want = (database or "").strip()
    if not want:
        return items
    hits = [db for db in items if str(db.get("Name", "")).strip().lower() == want.lower()]
    if not hits:
        names = ", ".join(str(db.get("Name", "")) for db in items) or "<none>"
        raise RuntimeError(
            f"AF database '{want}' not found on this AF server (available: {names}). "
            "Check the AF_Database field in the server list.")
    return hits


def discover_servers(base_url: str) -> pd.DataFrame:
    """Enumerate what a PI Web API endpoint exposes: PI Data Archive (DA)
    servers, AF servers and their databases.

    Endpoints: GET /dataservers and GET /assetservers (both are root links of
    the PI Web API home controller), then /assetservers/{webid}/assetdatabases.

    Returns ready-to-use server-list rows (same columns as
    MES_servers_list.xlsx): one row per DA server (plain point search) plus one
    row per AF server x database (attribute search). The first DA server is
    used as the companion `server` for AF rows (needed for DA lookups).
    """
    base = base_url.rstrip("/")
    s = get_session(base)

    r = s.get(f"{base}/dataservers",
              params={"selectedFields": "Items.Name;Items.IsConnected"}, timeout=60)
    _pi._log_url(r)
    r = check_response( r )
    da_names = [str(it.get("Name", "")) for it in r.json().get("Items", [])]

    rows: list[dict] = []
    for da in da_names:
        rows.append({"site": f"{da} (DA points)", "server": da, "Type": "PI",
                     "WebAPI_URL": base, "PI_AF_Server": "", "AF_Database": ""})

    r = s.get(f"{base}/assetservers",
              params={"selectedFields": "Items.Name;Items.WebId"}, timeout=60)
    _pi._log_url(r)
    r = check_response( r )
    default_da = da_names[0] if da_names else ""
    for af in r.json().get("Items", []):
        af_name = str(af.get("Name", ""))
        for db in _databases(base, af["WebId"]):
            db_name = str(db.get("Name", ""))
            rows.append({"site": f"{db_name} (AF: {af_name})", "server": default_da,
                         "Type": "PI", "WebAPI_URL": base,
                         "PI_AF_Server": af_name, "AF_Database": db_name})

    return pd.DataFrame(
        rows, columns=["site", "server", "Type", "WebAPI_URL", "PI_AF_Server", "AF_Database"])


# ---------------------------------------------------------------------------
# Attribute search (the AF-mode search bar)
# ---------------------------------------------------------------------------
SEARCH_PAGE = 1000        # attributes per elementattributes request
SEARCH_WORKERS = 5        # pages fetched in parallel — same pool size as the
                          # extraction workers (do not raise without notifying
                          # the PI administrator)
MAX_SEARCH_RESULTS = 10000  # safety cap: an empty name filter on a big AF
                            # database would otherwise page for many minutes
                            # (and the GUI tree could not display it anyway)


def _search_db_attributes(s, base: str, db_webid: str, nf: str,
                          budget: int) -> list[dict]:
    """Page through one database's elementattributes, SEARCH_WORKERS pages at
    a time (waves), stopping at the first short/empty page or at `budget`.

    The v4.0 loop fetched pages strictly one by one with no result cap, so an
    unfiltered search on a large database ran for 15+ minutes with no error —
    the PI Web API answers each deep startIndex page slowly, and there were
    thousands of them.
    """

    def _fetch(start_index: int) -> list[dict]:
        params = {
            "searchFullHierarchy": "true",
            "startIndex": start_index,
            "maxCount": SEARCH_PAGE,
            "selectedFields": "Items.WebId;Items.Name;Items.Path;Items.Description;"
                              "Items.DefaultUnitsName;Items.Type",
        }
        if nf:
            params["attributeNameFilter"] = nf
        r = s.get(f"{base}/assetdatabases/{db_webid}/elementattributes",
                  params=params, timeout=120)
        _pi._log_url(r)
        r = check_response(r)
        return r.json().get("Items", [])

    out: list[dict] = []
    next_start = 0
    first_wave = True
    while len(out) < budget:
        pages_left = -(-(budget - len(out)) // SEARCH_PAGE)  # ceil division
        # First wave is a single probe page: most filtered searches fit in one
        # page (or are empty), so going wide immediately would waste requests.
        # Once page 0 comes back full, later waves run SEARCH_WORKERS pages in
        # parallel — that is what makes a full unfiltered index fast.
        width = 1 if first_wave else max(1, min(SEARCH_WORKERS, pages_left))
        wave = [next_start + i * SEARCH_PAGE for i in range(width)]
        if len(wave) == 1:
            pages = [_fetch(wave[0])]
        else:
            with ThreadPoolExecutor(max_workers=len(wave)) as pool:
                pages = list(pool.map(_fetch, wave))
        short = False
        for pg in pages:
            out.extend(pg)
            if len(pg) < SEARCH_PAGE:   # end of the result set is inside this wave
                short = True
                break
        if short:
            break
        next_start = wave[-1] + SEARCH_PAGE
        first_wave = False
        time.sleep(PAGING_DELAY_S)
    return out[:budget]


def search_attributes(base_url: str, af_server: str, name_filter: str = "",
                      description_filter: str = "",
                      max_results: int | None = None,
                      database: str = "") -> pd.DataFrame:
    """Search attributes across the AF server's databases.

    v4.1: `database` (server list AF_Database field) restricts the search to
    one database — recommended, since AF servers commonly host many. Results
    are capped at MAX_SEARCH_RESULTS (a warning is printed to the JMP log when
    the cap is hit — refine the name filter or scope the database).

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

    cap = MAX_SEARCH_RESULTS if max_results is None else int(max_results)
    rows: list[dict] = []
    for db in _databases(base, af_webid, database):
        t0 = time.monotonic()
        db_rows = _search_db_attributes(s, base, db["WebId"], nf, cap - len(rows))
        rows.extend(db_rows)
        print(f"[af-search] {db.get('Name', '?')}: {len(db_rows)} attributes "
              f"in {time.monotonic() - t0:.1f}s", flush=True)
        if len(rows) >= cap:
            print(f"[af-search] RESULT CAP REACHED ({cap} attributes) - the list "
                  "is truncated. Refine the tag name filter, or set AF_Database "
                  "in the server list to scope the search.", flush=True)
            break

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
def _element_levels(elem_path: str) -> list[str]:
    r"""Hierarchy levels of an element path, database excluded.

    \\AFSRV\DB\Area\Line\Reactor A -> ["Area", "Line", "Reactor A"]
    (the first two segments are the AF server and the database — they are the
    same for every row of an extraction, so they carry no information).
    """
    segs = [p for p in elem_path.lstrip("\\").split("\\") if p]
    if len(segs) > 2:
        return segs[2:]
    return segs[-1:] if segs else []


def extract_assets(base_url: str, attribute_paths: list[str], method: str,
                   start: str, end: str, interval_s: int,
                   filter_expression: str = "") -> pd.DataFrame:
    """Extract AF attributes stacked (concatenated) per asset — long format.

    Output: TS, TS_UTC, Level 1..Level K, Asset, <one column per attribute NAME>.
    - `Level i` columns hold the AF element hierarchy below the database
      (v4.1 — multi-level parent/child, e.g. Area / Line / Reactor A); K is
      the deepest selected element, shallower elements leave the extra levels
      empty. `Asset` repeats the leaf element name (level-agnostic row filter).
    - Attributes are grouped by their parent element (the asset); each asset's
      attributes are extracted on the same aligned grid and become one block
      of rows. Blocks are concatenated vertically (JMP Tables > Concatenate
      semantics): with 3 reactors the timestamps appear 3 times, once per
      reactor.
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

    levels = {elem: _element_levels(elem) for elem in assets}
    n_levels = max((len(v) for v in levels.values()), default=1)
    level_cols = [f"Level {i + 1}" for i in range(n_levels)]

    def _one_asset(elem: str, attrs: dict[str, str]) -> pd.DataFrame:
        paths = list(attrs.values())
        labels = list(attrs.keys())
        wide = _pi.extract(base, "", paths, labels, method, start, end,
                           int(interval_s), filter_expression)
        lv = levels[elem]
        for i, col in enumerate(level_cols):
            wide[col] = lv[i] if i < len(lv) else ""
        wide["Asset"] = elem.rpartition("\\")[2] or elem
        # union of columns: add the attributes this asset does not have
        for a in all_attrs:
            if a not in wide.columns:
                wide[a] = np.nan
        return wide[["TS", "TS_UTC"] + level_cols + ["Asset"] + all_attrs]

    parts = []
    for elem in assets:  # assets sequentially; tags within an asset in parallel
        parts.append(_one_asset(elem, assets[elem]))

    out = pd.concat(parts, axis=0, ignore_index=True)
    return (out.sort_values(level_cols + ["Asset", "TS_UTC"], kind="stable")
               .reset_index(drop=True))
