# -*- coding: utf-8 -*-
"""
mes_connector — REST extraction layer for the JMP MES Data Retrieval add-in.

Called from JSL (JMP 19 embedded Python) via `include/Ressources_REST.jsl`.
See README.md in this folder for the architecture and the JSL/Python contracts.

Public API (everything JSL calls lives here):
    set_credentials(host, user, password)   # from the JSL login dialog
    test_connection(server_type, base_url, da_server)
    search_tags(server_type, base_url, da_server, name, description)
    pi_extract(base_url, da_server, tags, labels, method, start, end,
               interval_s, filters_json)
    ip21_sql(base_url, datasource, sql)

Conventions:
- server_type is "PI" or "IP21" (same strings the JSL GUI uses).
- All functions return pandas DataFrames (JMP converts them to data tables)
  or raise; auth.AuthRequired is caught by JSL to show a login dialog.
"""
from __future__ import annotations

import pandas as pd

from . import filters as _filters
from . import ip21_rest as _ip21
from . import pi_webapi as _pi
from .auth import AuthRequired, clear_credentials, set_credentials  # noqa: F401 (re-exported for JSL)
from .ip21_rest import ip21_sql  # noqa: F401 (re-exported for JSL)

__version__ = "3.0.0"


def test_connection(server_type: str, base_url: str, da_server: str) -> str:
    """Cheap round-trip to verify the REST endpoint answers.

    Returns "OK" (JSL checks for this literal) or raises. Used by the JSL
    dispatcher to decide REST vs the Windows OLEDB fallback.
    """
    if server_type == "PI":
        _pi._dataserver_webid(base_url.rstrip("/"), da_server)
    else:
        _ip21.browse(base_url, da_server, tag_wildcard="*", max_tags=1)
    return "OK"


def search_tags(server_type: str, base_url: str, da_server: str,
                name: str = "", description: str = "") -> pd.DataFrame:
    """Tag search; columns: tagnames, descriptions, units, type (JSL contract)."""
    if server_type == "PI":
        return _pi.search_tags(base_url, da_server, name, description)
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
    return _ip21.ip21_sql(base_url, da_server, sql)


def pi_extract(base_url: str, da_server: str, tags: list[str], labels: list[str],
               method: str, start: str, end: str, interval_s: int,
               filters_json: str = "") -> pd.DataFrame:
    """PI extraction with the add-in's registered filters applied.

    Filter strategy (v3.0 decision): push simple comparisons server-side as a
    PI filterExpression; apply Like/Not Like/In locally in pandas. Both keep
    every tag on the same timestamp grid (see filters.py).
    """
    flt, condition = _filters.parse_filters_json(filters_json)

    filter_expression = ""
    if flt and _filters.can_push_server_side(flt):
        filter_expression = _filters.build_filter_expression(flt, condition)

    wide = _pi.extract(base_url, da_server, tags, labels, method,
                       start, end, interval_s, filter_expression)

    if flt and not filter_expression:
        wide = _filters.apply_local_filters(wide, flt, condition)

    return wide


def pi_distinct_values(base_url: str, da_server: str, tag: str,
                       start: str, end: str, interval_s: int = 60) -> pd.DataFrame:
    """Distinct values of one tag over a window (GUI 'Preview' button).

    Returns columns NAME / VALUE like the legacy SQL DISTINCT preview.
    """
    wide = _pi.extract(base_url, da_server, [tag], ["VALUE"], "Interpolated",
                       start, end, int(interval_s))
    vals = wide["VALUE"].dropna().unique() if "VALUE" in wide.columns else []
    return pd.DataFrame({"NAME": [tag] * len(vals), "VALUE": [str(v) for v in vals]})
