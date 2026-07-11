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
from . import pi_af as _af
from . import pi_webapi as _pi
from .auth import (  # noqa: F401 (re-exported for JSL)
    AuthRequired,
    clear_credentials,
    configure_tls,
    forget_credentials,
    set_credentials,
)

__version__ = "4.1.0"


# ---------------------------------------------------------------------------
# v4.0 — PI Asset Framework (see pi_af.py for the endpoint documentation)
# ---------------------------------------------------------------------------
def search_af_attributes(base_url: str, af_server: str, name: str = "",
                         description: str = "", database: str = "",
                         tag: str = "", da_server: str = "") -> pd.DataFrame:
    """AF attribute search. Columns: tagnames (= full attribute path — the
    extraction identity), descriptions, units, type, path (element path),
    pointname (underlying PI tag). `name` filters the ATTRIBUTE name
    server-side; `tag` filters the underlying PI point client-side;
    `database` restricts the search to one AF database (AF_Database
    server-list field); `da_server` completes server-less ConfigStrings for
    the v4.1.5 tag-metadata lookup (empty descriptions/units are filled from
    the PI point's Descriptor/EngineeringUnits — see pi_af.search_attributes).
    The JSL side renders `path` as an element tree."""
    base = normalize_base_url("PI", base_url)
    return _af.search_attributes(base, af_server, name, description,
                                 database=database, tag_filter=tag,
                                 da_server=da_server)


def pi_discover(base_url: str) -> pd.DataFrame:
    """v4.1: enumerate DA servers, AF servers and AF databases exposed by a
    PI Web API endpoint (GET /dataservers + /assetservers + their databases).
    Returns ready server-list rows: site / server / Type / WebAPI_URL /
    PI_AF_Server / AF_Database. v4.1.5: each AF database row is paired with
    the DA server its attributes actually reference (sampled from PI-Point
    ConfigStrings), not blindly with the first exposed DA server — the GUI's
    'Load servers from Web API' button."""
    base = normalize_base_url("PI", base_url)
    return _af.discover_servers(base)


def pi_search(base_url: str, da_server: str, af_server: str = "",
              name: str = "", description: str = "",
              include_da: int = 1, af_database: str = "",
              af_only: int = 0, attribute: str = "") -> pd.DataFrame:
    """Combined PI search: AF attributes and/or plain DA points.

    v4.1 — two explicit modes driven by the GUI's "PI AF search" checkbox:
    - af_only=1: ATTRIBUTE search. Runs only against AF (restricted to
      `af_database` when given) and FAILS LOUDLY — no silent degradation to
      DA points, because the user explicitly asked for attributes and needs
      to see why the AF search failed (wrong server name, blocked endpoint,
      missing database...). Field mapping (v4.1.3): `attribute` filters the
      attribute NAME (server-side); `name` filters the underlying PI POINT
      name (client-side — the API has no point filter); `description`
      filters the attribute description (client-side).
    - af_only=0: plain DA point search, exactly like v3 (`name` = tag name,
      `attribute` ignored). The v4.0 behavior (AF automatically searched when
      configured, degrade to DA on failure) is kept only when include_da=1
      AND an AF server is configured, for backward compat with old reports.
    """
    if int(af_only):
        if not (af_server or "").strip():
            raise RuntimeError(
                "PI AF search requested but no PI_AF_Server is configured for "
                "this server (set it in the server list or 'Edit server address').")
        return _af.search_attributes(
            normalize_base_url("PI", base_url), af_server, attribute, description,
            database=af_database, tag_filter=name, da_server=da_server)

    parts: list[pd.DataFrame] = []
    af_error = None
    if (af_server or "").strip() and int(include_da) == 0:
        # legacy v4.0 call shape (include_da=0 used to mean "AF only"):
        # keep searching AF but degrade gracefully
        try:
            parts.append(_af.search_attributes(
                normalize_base_url("PI", base_url), af_server, name, description,
                database=af_database, da_server=da_server))
        except AuthRequired:
            raise                     # credentials issue: the dialog must handle it
        except Exception as ex:       # noqa: BLE001 - degrade, don't die
            af_error = ex
            print(f"[search] AF attribute search failed ({ex}) - falling back "
                  "to the plain DA point search. Check the PI_AF_Server value "
                  "in the server list if AF results were expected.", flush=True)
    if int(include_da) or af_error is not None or not (af_server or "").strip():
        try:
            parts.append(_pi.search_tags(
                normalize_base_url("PI", base_url), da_server, name, description))
        except Exception:
            if not parts:             # nothing else succeeded: surface it
                raise
    if not parts:
        return pd.DataFrame(columns=["tagnames", "descriptions", "units", "type",
                                     "path", "pointname"])
    out = pd.concat(parts, ignore_index=True)
    if "path" not in out.columns:
        out["path"] = ""
    # DA points ARE their own point: fill pointname so the GUI/metadata can
    # rely on the column existing for every row
    if "pointname" not in out.columns:
        out["pointname"] = out["tagnames"]
    else:
        out["pointname"] = out["pointname"].fillna(out["tagnames"])
    return out


def pi_tag_or_attribute_type(base_url: str, da_server: str, name_or_path: str) -> str:
    """Value type for a DA point or an AF attribute path (leading '\\\\')."""
    base = normalize_base_url("PI", base_url)
    if name_or_path.startswith("\\\\"):
        return _af.attribute_type(base, name_or_path)
    df = _pi.search_tags(base, da_server, name_or_path, "", max_results=50)
    df = df[df["tagnames"] == name_or_path]
    return str(df["type"].iloc[0]) if len(df) else ""


def search_event_frames(base_url: str, af_server: str, name: str = "",
                        template: str = "", start: str = "*-30d",
                        end: str = "*", database: str = "") -> pd.DataFrame:
    """Event frames overlapping [start, end]: Name/Template/Start/End/Path.
    v4.1.5: `database` scopes the search to one AF database (AF_Database
    server-list field), matching the attribute search's scoping."""
    base = normalize_base_url("PI", base_url)
    return _af.search_event_frames(base, af_server, name, template, start, end,
                                   database=database)


def _parse_event_frames_json(ef_json: str) -> list[dict]:
    import json
    if not ef_json or not ef_json.strip():
        return []
    return json.loads(ef_json).get("frames", [])


def pi_extract_assets(base_url: str, attribute_paths: list[str], method: str,
                      start: str, end: str, interval_s: int,
                      filters_json: str = "", ef_json: str = "") -> pd.DataFrame:
    """Asset-stacked extraction: TS, TS_UTC, Asset, <attribute name columns>.

    One block of rows per asset (JMP Concatenate semantics — timestamps are
    repeated per asset); missing attributes become missing values. Registered
    filters and selected event frames are applied to the stacked table.
    """
    base = normalize_base_url("PI", base_url)
    flt, condition, fe = _prepare_filters(filters_json)
    out = _af.extract_assets(base, list(attribute_paths), method, start, end,
                             int(interval_s), fe)
    if flt and not fe:
        out = _filters.apply_local_filters(out, flt, condition)
    ef = _parse_event_frames_json(ef_json)
    if ef:
        out = _af.apply_event_frames(out, ef)
    return out


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
               filters_json: str = "", ef_json: str = "") -> pd.DataFrame:
    """One-shot PI extraction with the add-in's registered filters applied."""
    base = normalize_base_url("PI", base_url)
    flt, condition, fe = _prepare_filters(filters_json)
    wide = _pi.extract(base, da_server, tags, labels, method, start, end, interval_s, fe)
    if flt and not fe:
        wide = _filters.apply_local_filters(wide, flt, condition)
    ef = _parse_event_frames_json(ef_json)
    if ef:
        wide = _af.apply_event_frames(wide, ef)
    return wide


# Chunked variant: JSL drives a loop of small chunks so it can update its
# progress bar between chunks (Python can't call back into a running JSL loop).
_extract_session: dict = {}


def pi_extract_begin(base_url: str, da_server: str, method: str, start: str,
                     end: str, interval_s: int, filters_json: str = "",
                     ef_json: str = "") -> str:
    flt, condition, fe = _prepare_filters(filters_json)
    _extract_session.clear()
    _extract_session.update(
        base=normalize_base_url("PI", base_url), da=da_server, method=method,
        start=start, end=end, interval=int(interval_s),
        flt=flt, condition=condition, fe=fe, parts=[],
        ef=_parse_event_frames_json(ef_json),
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
    if s.get("ef"):
        out = _af.apply_event_frames(out, s["ef"])
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
