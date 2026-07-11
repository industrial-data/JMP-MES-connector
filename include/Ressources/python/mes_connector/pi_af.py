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
- /assetdatabases/{webid}/elements                  -> element hierarchy
      (searchFullHierarchy=true, WebId+Path only — cheap: no attribute loading)
- /elements/{webid}/attributes                      -> per-element attributes,
      name-filtered server-side; fanned out over SEARCH_WORKERS threads.
      Together these two power the GUI search bar in AF mode. Deliberately
      NOT /assetdatabases/{id}/elementattributes: that traversal loads every
      attribute of the whole hierarchy per request (and per page!) and took
      15-30 minutes on production-size databases.
- /attributes?path=\\\\AF\\DB\\Element|Attribute    -> attribute WebId + Type
- /points/multiple?path=\\\\DA\\tag&path=...        -> tag metadata (Name /
      Descriptor / EngineeringUnits) for the PI points behind the attributes,
      many points per request, requests strictly SEQUENTIAL (v4.1.5).
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
from .auth import AuthRequired, check_response, get_session

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


DISCOVER_SAMPLE_ELEMENTS = 8  # elements probed per database for the DA pairing


def _db_dataserver(s, base: str, db_webid: str) -> str:
    r"""DA server whose tags a database's attributes actually reference,
    sampled from the first PI-Point ConfigString found (`\\SRV\tag` -> SRV).

    One element listing plus at most DISCOVER_SAMPLE_ELEMENTS attribute
    lookups, all SEQUENTIAL — discovery must stay light on the server.
    Returns '' when the sample holds no PI-Point reference.
    """
    r = s.get(f"{base}/assetdatabases/{db_webid}/elements",
              params={"searchFullHierarchy": "true", "maxCount": 100,
                      "selectedFields": "Items.WebId"}, timeout=60)
    _pi._log_url(r)
    r = check_response(r)
    for elem in r.json().get("Items", [])[:DISCOVER_SAMPLE_ELEMENTS]:
        r = s.get(f"{base}/elements/{elem['WebId']}/attributes",
                  params={"searchFullHierarchy": "true", "maxCount": 100,
                          "selectedFields":
                              "Items.ConfigString;Items.DataReferencePlugIn"},
                  timeout=60)
        _pi._log_url(r)
        r = check_response(r)
        for it in r.json().get("Items", []):
            path, _ = _point_path_and_name(
                it.get("ConfigString"), it.get("DataReferencePlugIn"))
            if path:
                return path.lstrip("\\").split("\\", 1)[0]
        time.sleep(PAGING_DELAY_S)
    return ""


def discover_servers(base_url: str) -> pd.DataFrame:
    """Enumerate what a PI Web API endpoint exposes: PI Data Archive (DA)
    servers, AF servers and their databases.

    Endpoints: GET /dataservers and GET /assetservers (both are root links of
    the PI Web API home controller), then /assetservers/{webid}/assetdatabases.
    One endpoint failing (blocked, not licensed) only drops its rows — the
    other side is still discovered.

    Returns ready-to-use server-list rows (same columns as
    MES_servers_list.xlsx): one row per DA server (plain point search) plus one
    row per AF server x database (attribute search). v4.1.5: each AF row's
    companion `server` is the DA server its attributes actually reference
    (sampled via _db_dataserver) — the list string then pairs the database
    with the RIGHT data server, not blindly with the first one. When the
    sample is inconclusive the first exposed DA server is used.
    """
    base = base_url.rstrip("/")
    s = get_session(base)

    rows: list[dict] = []
    da_names: list[str] = []
    try:
        r = s.get(f"{base}/dataservers",
                  params={"selectedFields": "Items.Name;Items.IsConnected"}, timeout=60)
        _pi._log_url(r)
        r = check_response( r )
        da_names = [str(it.get("Name", "")) for it in r.json().get("Items", [])]
    except AuthRequired:
        raise
    except Exception as ex:  # noqa: BLE001 - AF-only endpoints still discoverable
        print(f"[discover] /dataservers failed ({ex}) - no DA rows", flush=True)
    for da in da_names:
        rows.append({"site": f"{da} (DA points)", "server": da, "Type": "PI",
                     "WebAPI_URL": base, "PI_AF_Server": "", "AF_Database": ""})

    af_items: list[dict] = []
    try:
        r = s.get(f"{base}/assetservers",
                  params={"selectedFields": "Items.Name;Items.WebId"}, timeout=60)
        _pi._log_url(r)
        r = check_response( r )
        af_items = r.json().get("Items", [])
    except AuthRequired:
        raise
    except Exception as ex:  # noqa: BLE001 - DA-only endpoints still discoverable
        print(f"[discover] /assetservers failed ({ex}) - no AF rows", flush=True)

    for af in af_items:
        af_name = str(af.get("Name", ""))
        try:
            dbs = _databases(base, af["WebId"])
        except AuthRequired:
            raise
        except Exception as ex:  # noqa: BLE001 - continue with the other AF servers
            print(f"[discover] databases of AF server '{af_name}' failed ({ex})",
                  flush=True)
            continue
        for db in dbs:
            db_name = str(db.get("Name", ""))
            da = ""
            try:
                da = _db_dataserver(s, base, db["WebId"])
            except AuthRequired:
                raise
            except Exception as ex:  # noqa: BLE001 - pairing is best-effort
                print(f"[discover] DA sampling of '{db_name}' failed ({ex})",
                      flush=True)
            if da:
                print(f"[discover] {af_name}\\{db_name}: tags on '{da}'", flush=True)
            else:
                da = da_names[0] if da_names else ""
                print(f"[discover] {af_name}\\{db_name}: no PI-Point reference "
                      f"in the sample - paired with '{da}'", flush=True)
            rows.append({"site": f"{db_name} (AF: {af_name})", "server": da,
                         "Type": "PI", "WebAPI_URL": base,
                         "PI_AF_Server": af_name, "AF_Database": db_name})

    return pd.DataFrame(
        rows, columns=["site", "server", "Type", "WebAPI_URL", "PI_AF_Server", "AF_Database"])


# ---------------------------------------------------------------------------
# Attribute search (the AF-mode search bar)
# ---------------------------------------------------------------------------
SEARCH_PAGE = 1000        # items per paged request
SEARCH_WORKERS = 5        # parallel requests — same pool size as the
                          # extraction workers (do not raise without notifying
                          # the PI administrator)
MAX_SEARCH_RESULTS = 0    # 0 = UNLIMITED (v4.1.3): an unfiltered AF search
                          # must index EVERYTHING — the JSL side caps only the
                          # DISPLAY (AF_DisplayMax), never the data
MAX_SEARCH_ELEMENTS = 100000  # hierarchy-size guard (elements per database)
PROGRESS_EVERY = 250      # elements between progress prints to the JMP log


def _wild_match(text: str, pattern: str) -> bool:
    """Case-insensitive wildcard match, same semantics as the JSL search bar:
    spaces and '*' both split the pattern into tokens that must appear in the
    text IN ORDER ("tic 34" == "tic*34" == *tic*34*). Empty pattern matches."""
    tokens = [t for t in (pattern or "").lower().replace("*", " ").split() if t]
    hay = (text or "").lower()
    pos = 0
    for tok in tokens:
        hit = hay.find(tok, pos)
        if hit < 0:
            return False
        pos = hit + len(tok)
    return True


def _point_path_and_name(config: str, plugin: str, da_server: str = "") -> tuple[str, str]:
    r"""Underlying PI point of an attribute, from its data reference:
    (full `\\SRV\tag` path, bare tag name).

    `\\PIDA1\tic_abc3421;ReadOnly=1` -> (`\\PIDA1\tic_abc3421`, `tic_abc3421`).
    A ConfigString without the server part is completed with `da_server` when
    known (path stays '' otherwise — the name alone is still searchable).
    ('', '') for non-PI-Point references (formulas, constants, table lookups...).
    """
    if plugin and "pi point" not in str(plugin).lower():
        return "", ""
    cfg = str(config or "").split(";")[0].strip()
    name = cfg.rpartition("\\")[2].strip()
    if not name:
        return "", ""
    if cfg.startswith("\\\\"):
        srv = cfg.lstrip("\\").split("\\", 1)[0].strip()
        return (f"\\\\{srv}\\{name}", name) if srv else ("", name)
    if da_server:
        return f"\\\\{da_server}\\{name}", name
    return "", name


POINT_META_CHUNK = 50     # point paths per GET /points/multiple request
MAX_POINT_META = 3000     # skip the tag-metadata lookup above this many points
                          # (an unfiltered whole-database index would need
                          # thousands of requests — narrow the search instead)


def _points_metadata(s, base: str, paths: list[str]) -> dict[str, dict]:
    r"""Tag metadata (Name / Descriptor / EngineeringUnits) for many PI points
    in few requests: GET /points/multiple resolves POINT_META_CHUNK full
    `\\SRV\tag` paths per call. Calls run strictly SEQUENTIALLY — speed comes
    from the batching, not from client-side parallelism (server load policy).

    Returns {lower-cased requested path -> point object}; points that fail to
    resolve (deleted tag, wrong server) are simply absent.
    """
    meta: dict[str, dict] = {}
    for i in range(0, len(paths), POINT_META_CHUNK):
        chunk = paths[i:i + POINT_META_CHUNK]
        params = [("selectedFields",
                   "Items.Identifier;Items.Object.Name;"
                   "Items.Object.Descriptor;Items.Object.EngineeringUnits")]
        params += [("path", p) for p in chunk]
        r = s.get(f"{base}/points/multiple", params=params, timeout=120)
        _pi._log_url(r)
        r = check_response(r)
        items = r.json().get("Items", [])
        for j, it in enumerate(items):
            obj = it.get("Object") or {}
            if not obj:
                continue
            key = str(it.get("Identifier") or
                      (chunk[j] if j < len(chunk) else "")).lower()
            if key:
                meta[key] = obj
        time.sleep(PAGING_DELAY_S)
    return meta


def _list_elements(s, base: str, db_webid: str, cap: int) -> list[dict]:
    """All elements of a database (WebId + Path), wave-parallel paging.

    Element enumeration does NOT load attribute objects, so it is far cheaper
    than the elementattributes traversal (see _search_db_attributes)."""

    def _fetch(start_index: int) -> list[dict]:
        r = s.get(f"{base}/assetdatabases/{db_webid}/elements",
                  params={"searchFullHierarchy": "true",
                          "startIndex": start_index,
                          "maxCount": SEARCH_PAGE,
                          "selectedFields": "Items.WebId;Items.Path"},
                  timeout=120)
        _pi._log_url(r)
        r = check_response(r)
        return r.json().get("Items", [])

    out: list[dict] = []
    next_start = 0
    first_wave = True
    while len(out) < cap:
        pages_left = -(-(cap - len(out)) // SEARCH_PAGE)  # ceil division
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
            if len(pg) < SEARCH_PAGE:
                short = True
                break
        if short:
            break
        next_start = wave[-1] + SEARCH_PAGE
        first_wave = False
        time.sleep(PAGING_DELAY_S)
    return out[:cap]


def _search_db_attributes(s, base: str, db_webid: str, nf: str,
                          budget: int) -> list[dict]:
    """Attributes of one database: enumerate ELEMENTS, then ask each element
    for its (name-filtered) attributes — many small fast requests fanned out
    over SEARCH_WORKERS threads.

    Why not /assetdatabases/{id}/elementattributes?searchFullHierarchy=true?
    That call makes the AF server walk the WHOLE hierarchy and LOAD EVERY
    ATTRIBUTE on every request — and startIndex paging restarts the walk per
    page. On large databases a single page took minutes (observed: 15-30 min
    searches), and no amount of client-side parallelism fixes a per-request
    server-side traversal. Element enumeration skips the attribute loading,
    and /elements/{id}/attributes are cheap direct lookups.
    """
    t0 = time.monotonic()
    elements = _list_elements(s, base, db_webid, MAX_SEARCH_ELEMENTS)
    print(f"[af-search] hierarchy: {len(elements)} elements "
          f"in {time.monotonic() - t0:.1f}s", flush=True)
    if len(elements) >= MAX_SEARCH_ELEMENTS:
        print(f"[af-search] ELEMENT CAP REACHED ({MAX_SEARCH_ELEMENTS}) - deeper "
              "parts of the hierarchy are not scanned. Set AF_Database (or use "
              "a smaller database) to scope the search.", flush=True)

    def _element_attributes(elem: dict) -> list[dict]:
        params = {
            "searchFullHierarchy": "true",   # include nested (child) attributes
            "maxCount": SEARCH_PAGE,
            # ConfigString + DataReferencePlugIn expose the underlying PI point
            # (tag) name — there is no server-side filter for it, so the
            # tagname search is applied client-side on the parsed point name
            "selectedFields": "Items.WebId;Items.Name;Items.Path;Items.Description;"
                              "Items.DefaultUnitsName;Items.Type;"
                              "Items.ConfigString;Items.DataReferencePlugIn",
        }
        if nf:
            params["nameFilter"] = nf
        r = s.get(f"{base}/elements/{elem['WebId']}/attributes",
                  params=params, timeout=60)
        _pi._log_url(r)
        r = check_response(r)
        return r.json().get("Items", [])

    out: list[dict] = []
    done = 0
    # waves keep the output deterministic (element order) and allow stopping
    # at the budget without flooding the server with already-useless requests
    for w0 in range(0, len(elements), SEARCH_WORKERS * 5):
        wave = elements[w0:w0 + SEARCH_WORKERS * 5]
        with ThreadPoolExecutor(max_workers=min(SEARCH_WORKERS, len(wave))) as pool:
            for items in pool.map(_element_attributes, wave):
                out.extend(items)
        done += len(wave)
        if done % PROGRESS_EVERY < SEARCH_WORKERS * 5 and done < len(elements):
            print(f"[af-search] {done}/{len(elements)} elements scanned, "
                  f"{len(out)} attributes so far "
                  f"({time.monotonic() - t0:.0f}s)", flush=True)
        if len(out) >= budget:
            break
        time.sleep(PAGING_DELAY_S)
    return out[:budget]


def search_attributes(base_url: str, af_server: str, name_filter: str = "",
                      description_filter: str = "",
                      max_results: int | None = None,
                      database: str = "",
                      tag_filter: str = "",
                      da_server: str = "") -> pd.DataFrame:
    """Search attributes across the AF server's databases.

    Filters (v4.1.3 — matching the GUI's three search fields):
    - name_filter    -> ATTRIBUTE name, pushed server-side (nameFilter).
    - tag_filter     -> underlying PI POINT (tag) name. The PI Web API cannot
      filter by point server-side, so this is applied client-side on the
      point name parsed from each attribute's ConfigString.
    - description_filter -> attribute description, client-side. Applied AFTER
      the tag-metadata enrichment so it also matches tag descriptors.
    Client-side filters use the same wildcard semantics as the JSL search
    bars: spaces and '*' are in-order wildcards.

    `database` (server list AF_Database field) restricts the search to one
    database — recommended, since AF servers commonly host many.

    `da_server` (server list `server` field) completes ConfigStrings written
    without the `\\\\SRV` part so their tag metadata can be resolved too.

    max_results None/0 = UNLIMITED: an unfiltered search indexes the whole
    database (the GUI caps only what the tree DISPLAYS).

    v4.1.5 — tag metadata: AF attributes often carry no Description/UOM while
    the PI point behind them has a Descriptor and EngineeringUnits. After the
    scan, the points are resolved in bulk (sequential /points/multiple calls,
    capped at MAX_POINT_META distinct points) and empty descriptions/units are
    filled from the tag; `pointname` gets the tag's canonical casing.

    Returns the GUI contract columns: tagnames / descriptions / units / type /
    path / pointname — `tagnames` is the FULL attribute path (unique identity
    used for extraction), `path` the element path, `pointname` the underlying
    PI tag ('' for formula/constant/table attributes).
    """
    base = base_url.rstrip("/")
    af_webid = _af_server_webid(base, af_server)
    s = get_session(base)

    nf = (name_filter or "").strip()
    if nf and "*" not in nf and "?" not in nf:
        nf = f"*{nf}*"

    cap = int(max_results) if max_results else 10 ** 9  # 0/None = unlimited
    t_total = time.monotonic()
    rows: list[dict] = []
    for db in _databases(base, af_webid, database):
        t0 = time.monotonic()
        db_rows = _search_db_attributes(s, base, db["WebId"], nf, cap - len(rows))
        rows.extend(db_rows)
        print(f"[af-search] {db.get('Name', '?')}: {len(db_rows)} attributes "
              f"in {time.monotonic() - t0:.1f}s", flush=True)
        if len(rows) >= cap:
            print(f"[af-search] result cap reached ({cap} attributes) - the "
                  "list is truncated.", flush=True)
            break
    print(f"[af-search] TOTAL: {len(rows)} attributes "
          f"in {time.monotonic() - t_total:.1f}s", flush=True)

    empty_cols = ["tagnames", "descriptions", "units", "type", "path", "pointname"]
    if not rows:
        return pd.DataFrame(columns=empty_cols)

    df = pd.DataFrame(rows)
    for src, dst in [("Path", "tagnames"), ("Description", "descriptions"),
                     ("DefaultUnitsName", "units"), ("Type", "type")]:
        df[dst] = df.get(src, "")
        df[dst] = df[dst].fillna("")
    # element path (everything before the |attribute part)
    df["path"] = df["tagnames"].str.split("|").str[0]
    # underlying PI point (tag), parsed from the data reference: full
    # \\SRV\tag path (for the metadata lookup) + bare name (search/display)
    cfg = df.get("ConfigString", pd.Series("", index=df.index)).fillna("")
    plugin = df.get("DataReferencePlugIn", pd.Series("", index=df.index)).fillna("")
    pts = [_point_path_and_name(c, p, da_server) for c, p in zip(cfg, plugin)]
    df["pointpath"] = [pp for pp, _ in pts]
    df["pointname"] = [pn for _, pn in pts]

    tg = (tag_filter or "").strip()
    if tg:
        df = df[df["pointname"].map(lambda x: _wild_match(x, tg))]
        print(f"[af-search] tagname filter '{tg}': {len(df)} attributes match "
              "an underlying PI point (client-side - the Web API cannot filter "
              "by point)", flush=True)

    df = df.drop_duplicates(subset="tagnames")

    # v4.1.5: tag metadata — fill empty attribute descriptions/units from the
    # PI point's Descriptor/EngineeringUnits (resolved in bulk, sequentially)
    point_paths = sorted({p for p in df["pointpath"] if p})
    if point_paths and len(point_paths) <= MAX_POINT_META:
        t0 = time.monotonic()
        try:
            meta = _points_metadata(s, base, point_paths)
        except AuthRequired:
            raise
        except Exception as ex:  # noqa: BLE001 - enrichment must not kill the search
            meta = {}
            print(f"[af-search] tag metadata lookup failed ({ex}) - showing "
                  "AF metadata only", flush=True)
        if meta:
            def _tag_field(path: str, field: str) -> str:
                return str((meta.get(path.lower()) or {}).get(field) or "")

            df["pointname"] = [_tag_field(pp, "Name") or pn
                               for pp, pn in zip(df["pointpath"], df["pointname"])]
            df["descriptions"] = [
                ds if str(ds).strip() else _tag_field(pp, "Descriptor")
                for ds, pp in zip(df["descriptions"], df["pointpath"])]
            df["units"] = [
                un if str(un).strip() else _tag_field(pp, "EngineeringUnits")
                for un, pp in zip(df["units"], df["pointpath"])]
            print(f"[af-search] tag metadata: {len(meta)}/{len(point_paths)} "
                  f"points resolved in {time.monotonic() - t0:.1f}s", flush=True)
    elif len(point_paths) > MAX_POINT_META:
        print(f"[af-search] {len(point_paths)} distinct PI points - skipping "
              f"the tag metadata lookup (cap {MAX_POINT_META}); narrow the "
              "search to get tag descriptions/units", flush=True)

    d = (description_filter or "").strip()
    if d:
        df = df[df["descriptions"].map(lambda x: _wild_match(x, d))]

    return df[empty_cols].reset_index(drop=True)


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
                        end: str = "*", max_results: int = 2000,
                        database: str = "") -> pd.DataFrame:
    """Event frames overlapping [start, end].

    v4.1.5: `database` (server list AF_Database field) restricts the search to
    that ONE database — same scoping as the attribute search, so a server-list
    entry consistently means "this AF database". Empty = all databases.

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
    for db in _databases(base, af_webid, database):
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
