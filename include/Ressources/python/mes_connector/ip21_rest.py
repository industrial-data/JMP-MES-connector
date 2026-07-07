# -*- coding: utf-8 -*-
"""
Aspen InfoPlus.21 access via the Aspen Process Data REST service (SQL mode).

The JSL side keeps building the exact same SQLplus queries as v2.x (search,
extraction, filters, preview) and sends the SQL TEXT here. This module wraps
it in Aspen's batch envelope, POSTs it to the /SQL passthrough endpoint, and
converts the JSON response into a DataFrame.

Base URL (from the WebAPI_URL column of the server list):
    http://<ip21-app-server>/ProcessData/AtProcessDataREST.dll
(some deployments are plain http, others https — the user's scheme is kept;
a bare host gets http:// by default, see normalize_base_url)

THE ENVELOPE (validated against a live server): the /SQL endpoint takes ONE
plain <SQL> XML element as the request body — nothing around it:

    <SQL c="DRIVER={AspenTech SQLplus};HOST=localhost;Port=10014;
            CHARINT=N;CHARFLOAT=N;CHARTIME=N;CONVERTERRORS=N"
         m="30000" to="90" s="1"><![CDATA[ ...SQL text... ]]></SQL>

CAUTION — do not add square brackets around it. The JSL reference script
looks like it sends [<SQL ...>] but that is an illusion: in JSL, "\[ ... ]\"
inside a double-quoted string is the RAW-STRING ESCAPE SYNTAX (the brackets
are string delimiters, not content). Sending a leading '[' makes the
server-side XmlLite parser fail with 200 + 'XML Error( Read ) WC_E_SYNTAX'.

- c  : the ODBC-style connection string used SERVER-SIDE by the REST service
       to reach its SQLplus engine. HOST=localhost is the standard: the REST
       dll and the SQLplus engine run on the same box, so the client never
       names the historian host here. CHARINT/CHARFLOAT/CHARTIME/
       CONVERTERRORS=N ask for native types instead of everything-as-text.
- m  : request timeout in milliseconds.
- to : connect timeout in seconds (headroom above m).
- s  : sequence number of this statement inside the batch (we send one).
(m/to/s semantics are inferred from Aspen's bundled samples; confirm against
 http://<server>/ProcessData/samples/sample_home.html for your version.)

THE RESPONSE is JSON, shaped long: data.rows[] each containing fld[] items
with {i: column-index, v: value} (and the column definitions under data.cols
when the server provides them). `_json_rows_to_dataframe` pivots that into
one row per record with the SELECT's column names — which is what the JSL
post-processing expects (tagnames/descriptions/... or NAME/TS/TS_UTC/...).
"""
from __future__ import annotations

import json

import pandas as pd

from .auth import check_response, get_session

REQUEST_TIMEOUT_MS = 30000   # m= : per-request timeout (server side), ms
CONNECT_TIMEOUT_S = 90       # to=: connect timeout (server side), s
HTTP_TIMEOUT_S = 300         # our own HTTP client timeout

# Server-side connection string: the REST service talks to the SQLplus engine
# on ITS OWN host (localhost) — do not put the historian hostname here.
CONNECTION_STRING = ("DRIVER={AspenTech SQLplus};HOST=localhost;Port=10014;"
                     "CHARINT=N;CHARFLOAT=N;CHARTIME=N;CONVERTERRORS=N")


def _sql_envelope(sql: str) -> str:
    """Wrap one SQL statement in Aspen's <SQL> envelope.

    NO surrounding brackets — see the module docstring: the [ ] seen in the
    JSL reference are JSL raw-string delimiters, not payload. A leading '['
    breaks the server's XML parser (200 + 'XML Error( Read ) WC_E_SYNTAX')."""
    return (
        f'<SQL c="{CONNECTION_STRING}" m="{REQUEST_TIMEOUT_MS}" '
        f'to="{CONNECT_TIMEOUT_S}" s="1"><![CDATA[{sql}]]></SQL>'
    )


def ip21_sql(base_url: str, datasource: str, sql: str) -> pd.DataFrame:
    """Run a SQLplus query through the REST /SQL passthrough.

    `datasource` is kept for call-site compatibility but is not part of the
    envelope: the REST service always reaches its local engine (HOST=localhost).
    Column names/order come from the SELECT itself, so results are identical
    to what the ODBC path produced — the JSL post-processing is unchanged.
    """
    base = base_url.rstrip("/")
    body = _sql_envelope(sql)
    s = get_session(base)
    # Echo the request in the JMP log (v2.x did the same with its PowerShell commands)
    print(f"POST {base}/SQL  sql={' '.join(sql.split())[:300]}", flush=True)
    r = s.post(
        f"{base}/SQL",
        data=body.encode("utf-8"),
        # the endpoint expects the envelope as the raw body; the working JSL
        # reference sent it under an application/json content type
        headers={"Content-Type": "application/json",
                 "X-Requested-With": "XMLHttpRequest"},
        timeout=HTTP_TIMEOUT_S,
    )
    print(f" -> {r.status_code}", flush=True)
    r = check_response( r )
    return _json_rows_to_dataframe(r.text)


def test_connection_sql(base_url: str, datasource: str) -> pd.DataFrame:
    """Cheap round-trip: a search that returns no rows but exercises the
    full envelope -> SQLplus -> JSON pipeline."""
    return ip21_sql(base_url, datasource,
                    "SELECT name as tagnames FROM all_records "
                    "WHERE name like 'ZZZ_MES_CONNECTOR_PROBE%';")


def _json_rows_to_dataframe(text: str) -> pd.DataFrame:
    """Pivot Aspen's long JSON result (rows[].fld[] of {i, v}) into a wide
    DataFrame with the SELECT's column names."""
    text = (text or "").strip()
    if not text:
        return pd.DataFrame()
    try:
        doc = json.loads(text)
    except ValueError as ex:
        # HTML error page, IIS auth page, proxy interception...
        raise RuntimeError(
            f"IP21 REST returned non-JSON ({ex}); response starts with: {text[:300]!r}"
        ) from ex

    data = doc.get("data", doc) if isinstance(doc, dict) else doc

    # Aspen reports SQL/engine errors inside the payload, not via HTTP status
    if isinstance(data, dict):
        for key in ("er", "err", "error", "Error"):
            msg = data.get(key)
            if msg:
                raise RuntimeError(f"IP21 REST error: {msg}")

    if not isinstance(data, dict):
        raise RuntimeError(f"IP21 REST: unexpected payload shape: {text[:300]!r}")

    # column index -> column name (from data.cols metadata when present)
    names: dict[int, str] = {}
    cols = data.get("cols") or data.get("columns") or []
    for idx, c in enumerate(cols, start=1):
        if isinstance(c, dict):
            names[int(c.get("i", idx))] = str(c.get("n") or c.get("name") or f"col_{idx}")

    records = []
    for row in data.get("rows", []):
        flds = row.get("fld", []) if isinstance(row, dict) else []
        rec = {}
        for f in flds:
            i = int(f.get("i", len(rec) + 1))
            # fld items sometimes carry their own name key; prefer metadata
            name = names.get(i) or str(f.get("n") or f"col_{i}")
            rec[name] = f.get("v")
        if rec:
            records.append(rec)

    df = pd.DataFrame(records)
    if not df.empty and all(str(c).startswith("col_") for c in df.columns):
        # No column metadata anywhere: surface it loudly — the JSL layer needs
        # the SELECT aliases (tagnames/TS/...) to keep working.
        print("[ip21] WARNING: response had no column names; got "
              f"{list(df.columns)} — check the /SQL response shape.", flush=True)
    return df
