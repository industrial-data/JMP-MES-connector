# mes_connector — Python extraction layer (v3.0)

Python package called by the JMP add-in (via JMP 19's embedded Python) to extract
historian data over REST instead of ODBC/OLEDB drivers. REST is the **default**
path; the legacy OLEDB/ODBC path remains as a **Windows-only fallback** (see
`include/Ressources_REST.jsl` for the dispatch logic on the JSL side).

## Architecture (read this first, agents)

```
JMP (JSL GUI)                          Python (this package)
─────────────                          ─────────────────────
Ressources_REST.jsl
  f_REST_FindTags ───► Python ───────► search_tags()  ──► PI:   GET /points?nameFilter=...
  f_REST_Extract  ───► Execute/Get ──► extract()      ──► PI:   GET /streams/{webid}/...
  f_IP21_REST_SQL ──────────────────► ip21_sql()      ──► IP21: POST AtProcessDataREST.dll/SQL
                                                            (wraps the UNCHANGED legacy
                                                             SQLplus queries built in JSL)
```

- **IP21**: the JSL still builds the exact same SQLplus queries as v2.x
  (including filters). Only the *transport* changed: instead of the AspenTech
  ODBC driver, the SQL text is POSTed to Aspen's Process Data REST service
  (`http://<server>/ProcessData/AtProcessDataREST.dll/SQL`). No driver install
  needed, ~5x faster (see `IP21_REST_API_design.docx` in the parent folder).
- **PI**: native PI Web API (`https://<server>/piwebapi`). Ported from the
  validated notebook `piwebapi_extraction_all_tags_async_credentials_indexing.ipynb`
  in the parent folder (search, interpolated/recorded/summary extraction, paging,
  retries, parallel workers). Filters: simple comparisons (=, <, >, <=, >=, !=)
  are pushed server-side as a PI `filterExpression` (Performance-Equation syntax,
  can reference other tags); `Like` / `Not Like` / `In` are applied locally in
  pandas after extraction (see `filters.py`).

## Modules

| file          | purpose |
|---------------|---------|
| `auth.py`     | requests.Session factory: Windows SSO (SSPI) / Kerberos when available, else basic auth injected from a JSL login dialog via `set_credentials()`. |
| `pi_webapi.py`| PI Web API: tag search, single/bulk extraction, aligned timestamp grid. |
| `ip21_rest.py`| Aspen Process Data REST: run a SQLplus query, return a DataFrame. |
| `filters.py`  | Translate the add-in's filter structure to PI filterExpression and/or pandas masks. |
| `__init__.py` | Public API used by JSL: `search_tags`, `extract`, `ip21_sql`, `set_credentials`, `test_connection`. |

## Contracts with the JSL side (do not break these)

- `search_tags()` returns a DataFrame with columns **exactly**
  `tagnames, descriptions, units, type` — same shape as the legacy SQL search,
  so the GUI list-box code is untouched.
- `extract()` returns a **wide** DataFrame: `TS` (server-local time, string
  `yyyy-MM-dd HH:mm:ss`), `TS_UTC`, then one column per requested tag whose
  column name is the *label* passed by JSL (tagname + description + unit).
- `ip21_sql()` returns the raw query result (long format) — the JSL
  post-processing pipeline (`f_PostProcess`) consumes it exactly as it consumed
  the ODBC result.
- Auth: any function may raise `AuthRequired`, which JSL catches to show a
  login dialog, then calls `set_credentials(host, user, password)` and retries.

## Dependencies

`requests`, `pandas` (bundled with JMP's Python). Optional, for SSO:
`requests-negotiate-sspi` (Windows), `requests-kerberos` (macOS/Linux).
Without them the code silently falls back to the credentials dialog.
