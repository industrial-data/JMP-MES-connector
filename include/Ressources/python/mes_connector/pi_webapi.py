# -*- coding: utf-8 -*-
"""
PI Web API access (OSIsoft / Aveva PI).

Ported from the validated notebook
`../piwebapi_extraction_all_tags_async_credentials_indexing.ipynb`
(same repo folder), simplified for the add-in use case:
- search_tags(): server-side nameFilter on /points (no full index cache needed
  for interactive GUI searches), local description filter.
- extract(): interpolated / recorded ("Actual") / summary ("Average") values
  for a list of tags, paged, retried, in parallel (max 5 workers — the safe
  limit agreed with PI admins in the notebook), merged on one aligned grid.

Key PI Web API endpoints used (see https://<server>/piwebapi/help):
- GET /dataservers?name=<da_server>            -> data server WebId
- GET /dataservers/{webid}/points              -> tag search (nameFilter=...)
- GET /streams/{webid}/interpolated            -> method "Interpolated"
- GET /streams/{webid}/recorded                -> method "Actual"
- GET /streams/{webid}/summary                 -> method "Average"

Timestamps: JSL passes start/end as 'yyyy-MM-ddTHH:mm:ss' (no timezone).
PI Web API interprets timezone-less times as *PI server local time*, which
matches the v2.x OLEDB behavior ("Time Zone = Server" in the connection
string), so the two paths return the same window.
"""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import numpy as np
import pandas as pd

from .auth import get_session, check_response

MAX_WORKERS = 5          # do not raise without notifying the PI administrator
PAGING_DELAY_S = 0.02    # small delay between pages to avoid server overload
MAX_ATTEMPTS = 3
RETRY_WAIT_S = 5

NUMERIC_POINT_TYPES = {
    "Float16", "Float32", "Float64", "Int8", "Int16", "Int32", "Int64",
    "UInt8", "UInt16", "UInt32", "UInt64", "Single", "Double",
}

# Every request URL is echoed with print() so it shows up in the JMP log —
# same debugging philosophy as v2.x, which echoed its PowerShell commands.
LOG_URLS = True


def _log_url(resp) -> None:
    if LOG_URLS:
        print(f"GET {resp.request.url} -> {resp.status_code}", flush=True)


# ---------------------------------------------------------------------------
# Data server + tag search
# ---------------------------------------------------------------------------
def _dataserver_webid(base: str, da_server: str) -> str:
    s = get_session(base)
    r = s.get(f"{base}/dataservers", params={"name": da_server}, timeout=60)
    _log_url(r)
    check_response(r)
    body = r.json()
    if "WebId" in body:
        return body["WebId"]
    items = body.get("Items", [])
    if items:
        return items[0]["WebId"]
    raise RuntimeError(f"PI Data Archive server '{da_server}' not found at {base}")


def search_tags(base_url: str, da_server: str, name_filter: str = "",
                description_filter: str = "", max_results: int | None = None) -> pd.DataFrame:
    """Search tags. Returns columns: tagnames, descriptions, units, type.

    (Same column contract as the legacy SQL search — the JSL GUI depends on it.)
    The GUI passes plain substrings; PI's nameFilter uses * wildcards, so we
    wrap: 'FIC' -> '*FIC*'. Description filtering is done locally because the
    /points endpoint only filters on name.

    UNCAPPED by default: pages through the full point database (1000 per
    request) until the server has no more matches. Pass max_results to limit.
    """
    base = base_url.rstrip("/")
    webid = _dataserver_webid(base, da_server)
    s = get_session(base)

    nf = (name_filter or "").strip()
    if nf and "*" not in nf and "?" not in nf:
        nf = f"*{nf}*"

    rows, start_index, page = [], 0, 1000
    while max_results is None or len(rows) < max_results:
        count = page if max_results is None else min(page, max_results - len(rows))
        params = {
            "startIndex": start_index,
            "maxCount": count,
            "selectedFields": "Items.Name;Items.Descriptor;Items.EngineeringUnits;Items.PointType;Items.Path",
        }
        if nf:
            params["nameFilter"] = nf
        r = s.get(f"{base}/dataservers/{webid}/points", params=params, timeout=120)
        _log_url(r)
        check_response(r)
        items = r.json().get("Items", [])
        if not items:
            break
        rows.extend(items)
        start_index += len(items)
        if len(items) < count:
            break
        time.sleep(PAGING_DELAY_S)

    df = pd.DataFrame(rows)
    if df.empty:
        return pd.DataFrame(columns=["tagnames", "descriptions", "units", "type"])

    df = df.rename(columns={
        "Name": "tagnames", "Descriptor": "descriptions",
        "EngineeringUnits": "units", "PointType": "type", "Path": "path",
    })
    for c in ("tagnames", "descriptions", "units", "type", "path"):
        if c not in df.columns:
            df[c] = ""
        df[c] = df[c].fillna("")

    d = (description_filter or "").strip().strip("*")
    if d:
        df = df[df["descriptions"].str.contains(d, case=False, na=False, regex=False)]

    # `path` is extra (not part of the legacy contract): kept for the planned
    # AF attribute search where labels become  tag (desc) [units] {path}
    return df[["tagnames", "descriptions", "units", "type", "path"]].reset_index(drop=True)


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------
def _point_info(base: str, da_webid: str, tag: str) -> dict:
    """WebId + PointType for one tag (exact name lookup)."""
    s = get_session(base)
    r = s.get(
        f"{base}/dataservers/{da_webid}/points",
        params={"nameFilter": tag, "maxCount": 1,
                "selectedFields": "Items.WebId;Items.Name;Items.PointType"},
        timeout=60,
    )
    _log_url(r)
    check_response(r)
    items = r.json().get("Items", [])
    if not items:
        raise RuntimeError(f"Tag '{tag}' not found on PI server")
    return items[0]


def _stream_url_and_params(base: str, webid: str, method: str, start: str, end: str,
                           interval_s: int, filter_expression: str) -> tuple[str, dict]:
    """Map the add-in's extraction method to the right streams endpoint.

    "Step Interpolated" fetches recorded values (boundaryType=Outside so the
    value in force at the window start is included) and the caller
    forward-fills them onto the aligned grid — true staircase interpolation.
    PI Web API's own /interpolated has no stepped override (it follows the
    point's Step attribute), so this is done client-side for all tags alike.
    """
    params: dict = {"startTime": start, "endTime": end}
    if method == "Actual":
        url = f"{base}/streams/{webid}/recorded"
        params["boundaryType"] = "Inside"
    elif method in ("Stepped", "Step Interpolated"):
        url = f"{base}/streams/{webid}/recorded"
        params["boundaryType"] = "Outside"
    elif method == "Average":
        url = f"{base}/streams/{webid}/summary"
        params.update({
            "summaryType": "Average",
            "summaryDuration": f"{interval_s}s",
            "timeType": "EarliestTime",  # stamp each average at interval start
        })
    else:  # Interpolated (default)
        url = f"{base}/streams/{webid}/interpolated"
        params["interval"] = f"{interval_s}s"
    if filter_expression:
        params["filterExpression"] = filter_expression
        params["includeFilteredValues"] = "false"
    return url, params


def _normalize_value(v, point_type: str):
    """Numeric tags -> float; digital/string tags -> readable text.

    PI digital states arrive as {'Name': ..., 'Value': ...} dicts.
    """
    if isinstance(v, dict):
        v = v.get("Name", v.get("Value"))
    if point_type in NUMERIC_POINT_TYPES:
        try:
            return float(v)
        except (TypeError, ValueError):
            return np.nan
    return None if v is None else str(v)


def _fetch_one(base: str, da_webid: str, tag: str, label: str, method: str,
               start: str, end: str, interval_s: int, filter_expression: str) -> pd.DataFrame:
    """One tag -> DataFrame indexed by timestamp with a single `label` column.

    Handles paging via Links.Next (present when a window exceeds the server's
    max return count) and unwraps summary items (nested one level deeper).
    """
    # Empty results must still carry a tz-aware DatetimeIndex, otherwise
    # concat with non-empty tags fails (cannot join tz-naive with tz-aware).
    empty = pd.DataFrame(columns=[label], index=pd.DatetimeIndex([], tz="UTC"))

    info = _point_info(base, da_webid, tag)
    point_type = str(info.get("PointType", ""))
    s = get_session(base)

    url, params = _stream_url_and_params(
        base, info["WebId"], method, start, end, interval_s, filter_expression)

    all_items = []
    while url:
        time.sleep(PAGING_DELAY_S)
        r = s.get(url, params=params, timeout=300)
        _log_url(r)
        check_response(r)
        body = r.json()
        items = body.get("Items", [])
        if method == "Average":
            # summary items: {"Type": "Average", "Value": {Timestamp, Value, Good}}
            items = [it.get("Value", {}) for it in items]
        all_items.extend(items)
        url = body.get("Links", {}).get("Next")
        params = None  # the Next link already carries the query string

    if not all_items:
        return empty

    df = pd.DataFrame(all_items)
    if "Good" in df.columns:  # drop bad-quality values (matches v2.x '??????' filter)
        df = df[df["Good"].fillna(True)]
    if df.empty or "Timestamp" not in df.columns:
        return empty
    ts = pd.to_datetime(df.get("Timestamp"), errors="coerce", utc=True)
    df = df.loc[ts.notna()].copy()
    if df.empty:
        return empty
    df[label] = df["Value"].apply(lambda v: _normalize_value(v, point_type))
    df.index = ts.loc[ts.notna()].dt.floor("s")
    df = df[~df.index.duplicated(keep="last")]
    return df[[label]]


def _step_grid(start: str, end: str, interval_s: int) -> pd.DatetimeIndex:
    """Aligned UTC grid for Step Interpolated. start/end are local wall-time
    strings (same convention as TS), converted through the client zone."""
    from datetime import datetime
    tz = datetime.now().astimezone().tzinfo
    lo = pd.Timestamp(start).tz_localize(tz).tz_convert("UTC")
    hi = pd.Timestamp(end).tz_localize(tz).tz_convert("UTC")
    return pd.date_range(lo, hi, freq=f"{int(interval_s)}s", tz="UTC", name="UTC_Index")


def extract(base_url: str, da_server: str, tags: list[str], labels: list[str],
            method: str, start: str, end: str, interval_s: int,
            filter_expression: str = "") -> pd.DataFrame:
    """Extract many tags into one wide table: TS, TS_UTC, <one column per label>.

    - TS/TS_UTC are tz-naive datetime columns (JMP converts them natively).
    - A failing tag is retried MAX_ATTEMPTS times, then left as an all-empty
      column so the output always contains every requested tag.
    - "Step Interpolated": recorded values are forward-filled onto one shared
      aligned grid (previous value held until the next change).
    """
    base = base_url.rstrip("/")
    da_webid = _dataserver_webid(base, da_server)
    step_grid = _step_grid(start, end, interval_s) if method in ("Stepped", "Step Interpolated") else None

    def _job(i: int) -> tuple[int, pd.DataFrame]:
        last_err = None
        for attempt in range(MAX_ATTEMPTS):
            try:
                one = _fetch_one(base, da_webid, tags[i], labels[i], method,
                                 start, end, interval_s, filter_expression)
                if step_grid is not None:
                    # staircase: place recorded values on the shared grid,
                    # holding the previous value until the next change
                    one = one.reindex(step_grid.union(one.index)).ffill().reindex(step_grid)
                return i, one
            except Exception as ex:  # noqa: BLE001 - retried, then surfaced as empty col
                from .auth import AuthRequired
                if isinstance(ex, AuthRequired):
                    raise  # bubble up immediately -> JSL login dialog
                last_err = ex
                time.sleep(RETRY_WAIT_S)
        print(f"[extract] '{tags[i]}' failed after {MAX_ATTEMPTS} attempts: {last_err}")
        return i, pd.DataFrame(columns=[labels[i]], index=pd.DatetimeIndex([], tz="UTC"))

    parts: list[pd.DataFrame | None] = [None] * len(tags)
    with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, max(1, len(tags)))) as pool:
        for fut in as_completed([pool.submit(_job, i) for i in range(len(tags))]):
            i, df = fut.result()
            parts[i] = df

    wide = pd.concat([p for p in parts if p is not None], axis=1, join="outer").sort_index()

    # Timestamp columns: index is tz-aware UTC; TS = local wall time. Returned
    # as tz-naive datetime64 so JMP receives real datetime columns (no string
    # parsing on the JSL side). Documented limitation: TS uses the JMP client's
    # time zone, which matches the plant zone in the standard deployment.
    from datetime import datetime
    idx = wide.index
    out = pd.DataFrame()
    out["TS"] = idx.tz_convert(datetime.now().astimezone().tzinfo).tz_localize(None)
    out["TS_UTC"] = idx.tz_localize(None)
    for c in wide.columns:
        out[c] = wide[c].values
    return out.reset_index(drop=True)
