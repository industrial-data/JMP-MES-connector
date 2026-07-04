# -*- coding: utf-8 -*-
"""
Aspen InfoPlus.21 access via the Aspen Process Data REST service.

Per `../IP21_REST_API_design.docx`: the REST service can WRAP the add-in's
existing SQLplus queries — no query rewrite, no ODBC driver install, and it
benchmarked ~5x faster than ODBC round-tripping. So the JSL side keeps building
the exact same SQL text as v2.x (search, extraction, filters, preview) and
simply sends it here instead of through `Create Database Connection`.

Base URL (from the WebAPI_URL column of the server list):
    http://<ip21server.company.com>/ProcessData/AtProcessDataREST.dll

Endpoints used:
- POST <base>/SQL     : execute a SQLplus query (the wrapper — main path)
- GET  <base>/Browse  : fast wildcard tag search (used for connection tests;
                        the GUI search goes through SQL to keep the exact
                        legacy result shape)

The SQL controller takes an XML envelope; `<SQL>` attributes:
    t="SQLplus"  query language
    ds="..."     data source name as registered in ADSA (the DAServer column;
                 usually the IP21 host name)
    m="..."      max rows
    to="..."     timeout in seconds
    s="1"        stream results
The response is XML rows: <NewDataSet><Table><col>val</col>...</Table>...
NOTE: attribute details can vary slightly across Aspen versions — if a POST
returns HTTP 400, capture the response text (it contains Aspen's error) and
check the AtProcessDataREST documentation for your version.
"""
from __future__ import annotations

import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape

import pandas as pd

from .auth import get_session, check_response

MAX_ROWS = 1_000_000   # same cap as the v2.x ODBC connection string
TIMEOUT_S = 300


def ip21_sql(base_url: str, datasource: str, sql: str) -> pd.DataFrame:
    """Run a SQLplus query through the REST wrapper; return rows as a DataFrame.

    Column names/order come from the SELECT itself, so results are identical
    to what the ODBC path produced — the JSL post-processing is unchanged.
    """
    base = base_url.rstrip("/")
    body = (
        f'<SQL t="SQLplus" ds="{escape(datasource, {chr(34): "&quot;"})}" '
        f'm="{MAX_ROWS}" to="{TIMEOUT_S}" s="1">'
        f"<![CDATA[{sql}]]></SQL>"
    )
    s = get_session(base)
    # Echo the request in the JMP log (v2.x did the same with its PowerShell commands)
    print(f"POST {base}/SQL  ds={datasource}  sql={' '.join(sql.split())[:300]}", flush=True)
    r = s.post(
        f"{base}/SQL",
        data=body.encode("utf-8"),
        headers={"Content-Type": "text/xml"},
        timeout=TIMEOUT_S + 30,
    )
    print(f" -> {r.status_code}", flush=True)
    check_response(r)
    return _xml_rows_to_dataframe(r.text)


def browse(base_url: str, datasource: str, tag_wildcard: str = "*",
           max_tags: int = 100) -> pd.DataFrame:
    """Fast tag browse (design doc: ~4s for '*' vs ~55s via SQL search).

    Kept mainly as a lightweight connection test; returns whatever columns
    the server provides for each matched tag.
    """
    base = base_url.rstrip("/")
    s = get_session(base)
    r = s.get(
        f"{base}/Browse",
        params={"dataSource": datasource, "tag": tag_wildcard,
                "max": max_tags, "getTrendable": 0},
        timeout=120,
    )
    print(f"GET {r.request.url} -> {r.status_code}", flush=True)
    check_response(r)
    return _xml_rows_to_dataframe(r.text)


def _xml_rows_to_dataframe(xml_text: str) -> pd.DataFrame:
    """Parse Aspen's XML row set generically: each repeated leaf-holding
    element becomes a row, its children become columns."""
    xml_text = xml_text.strip()
    if not xml_text:
        return pd.DataFrame()
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as ex:
        # Aspen (or a proxy) answered with non-XML, e.g. an HTML error page.
        # Surface the beginning of the payload — it usually names the problem.
        raise RuntimeError(
            f"IP21 REST returned non-XML ({ex}); response starts with: {xml_text[:300]!r}"
        ) from ex

    # Aspen wraps errors in the payload rather than HTTP status codes
    for err in root.iter():
        if err.tag.lower().endswith("error") and (err.text or "").strip():
            raise RuntimeError(f"IP21 REST error: {err.text.strip()}")

    rows = []
    for record in root.iter():
        children = list(record)
        # a "row" is an element whose children are all leaves with text
        if children and all(len(c) == 0 for c in children):
            rows.append({c.tag: (c.text or "").strip() for c in children})
    return pd.DataFrame(rows)
