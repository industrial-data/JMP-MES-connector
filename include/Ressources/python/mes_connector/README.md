# mes_connector — Python extraction layer (v3.0)

Python package called by the JMP add-in (via JMP 19's embedded Python) to extract
historian data over REST. REST is the **only** transport — the legacy OLEDB
(PI PowerShell) and ODBC (IP21 driver) paths were removed in v3.0, so no driver
installs are needed and Windows/macOS behave identically.

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

## Server list (simplified in v3.0)

`MES_servers_list.xlsx` columns: `site` (optional display name, falls back to
the server name), `server` (MANDATORY — PI Data Archive name or IP21 ADSA data
source name), `Type` (PI | IP21), `WebAPI_URL` (MANDATORY — tolerant to missing
scheme and trailing `/`; a bare host is completed to `/piwebapi` or
`/ProcessData/AtProcessDataREST.dll` by `normalize_base_url`; the IP21 URL must
end up pointing at that .dll), `PI_AF_Server` (optional, PI only — enables the
AF attribute search), `AF_Database` (optional, PI only, v4.1 — restrict the AF
search to one database of that AF server; strongly recommended when the AF
server hosts several databases).

## Authentication & credential security

Order of attempts: SSO (Windows SSPI / Kerberos) → credentials stored in the
**OS credential vault** (Windows Credential Manager / macOS Keychain, via
`keyring`) → JSL login dialog. The dialog's "Remember on this computer" box
writes the credentials to the vault so the user never types them again.

Compliance measures (see `auth.py` for the full list):
- secrets live only in the OS vault (encrypted at rest, per-user) and process
  memory — never in files, logs, JMP variables, or URLs;
- basic credentials are refused over plain `http://` (SSO is still allowed
  there — Kerberos never sends the password);
- TLS certificates are verified by default against the OS certificate store
  (`truststore`, so corporate CAs work); `int.TLSVerify = 0` in config.jsl is
  a logged opt-out for self-signed plant servers;
- a stored password that gets a 401 is deleted from the vault immediately.

## Debugging

Every HTTP request URL is `print()`ed so it appears in the JMP log (v2.x did
the same with its PowerShell commands). Look for `GET https://.../streams/...`
lines when an extraction misbehaves. Toggle with `pi_webapi.LOG_URLS`.

## v4.1: PI Asset Framework (pi_af.py)

The GUI's **"PI AF search" checkbox** drives the mode explicitly
(`pi_search(..., af_only=1)`): checked, the search returns ONLY attributes and
AF errors PROPAGATE (no silent degradation to DA points — that v4.0 behavior
hid every AF misconfiguration); unchecked, it is a plain DA point search like
v3. The search is restricted to ONE database when the server
list provides `AF_Database` (recommended; a typo'd database name raises with
the list of available ones). The search ENUMERATES ELEMENTS first
(`/assetdatabases/{id}/elements`, WebId+Path only — no attribute loading) and
then asks each element for its attributes (`/elements/{id}/attributes`),
fanned out over SEARCH_WORKERS (5) threads.
Do NOT go back to `/assetdatabases/{id}/elementattributes?searchFullHierarchy`:
that call walks the whole hierarchy AND loads every attribute per request
(and per page), which took 15-30 minutes on production databases.

Three search filters (v4.1.3, matching the GUI fields): **Attribute** ->
attribute name, pushed server-side (`nameFilter`); **Tagname** -> the
underlying PI POINT name, applied client-side on the point parsed from each
attribute's `ConfigString` (the Web API cannot filter by point — attributes
whose data reference is not a PI Point get pointname "" and never match);
**Description** -> client-side. Client-side filters use the JSL search-bar
wildcard semantics (spaces/'*' = in-order wildcards, `_wild_match`).
Retrieval is UNCAPPED (`max_results=0`): an unfiltered search indexes the
whole database; only the JSL tree display is capped (`AF_DisplayMax`,
"first x of y shown"). MAX_SEARCH_ELEMENTS (100000) guards runaway
hierarchies, and progress lines (`[af-search] n/m elements scanned...`,
final `TOTAL: n attributes in Xs`) land in the JMP log while it runs.
The DataFrame gains a `pointname` column (DA rows: pointname = tagname).

**Tag metadata** (v4.1.5): after the scan, the PI points behind the attributes
are resolved in bulk — `GET /points/multiple?path=\\SRV\tag&path=...`,
POINT_META_CHUNK (50) paths per call, calls strictly SEQUENTIAL (speed comes
from the batching, never from client-side parallelism). Empty attribute
descriptions/units are filled from the tag's `Descriptor`/`EngineeringUnits`
and `pointname` gets the tag's canonical casing, so each attribute shows its
associated tagname, tag description and tag unit. Server-less ConfigStrings
are completed with the entry's `server` (DA) field. Lookup is skipped (with a
log line) above MAX_POINT_META (3000) distinct points — narrow the search to
get tag metadata. The description filter runs AFTER the enrichment, so it
also matches tag descriptors.

Results are identified by the full AF path
(`\\AFSRV\DB\Plant\Reactor A|Temperature`). The JSL side renders them as an
element-hierarchy Tree Box (root = database) labeled
`attr {description} [units] {type}`; the path-suffixed label remains the
unique identity in the selection model. Attribute WebIds are streamable, so
extraction reuses the same /streams code (`_point_info` resolves paths via
`/attributes?path=`).

**Discovery** (`pi_discover`): `GET /dataservers` + `/assetservers` (+ each
server's databases) → ready server-list rows
(site/server/Type/WebAPI_URL/PI_AF_Server/AF_Database), one per DA server and
one per AF database. Feeds the GUI's "Load servers from Web API..." button.
v4.1.5: each AF database row's `server` is the DA server its attributes
actually reference — sampled from the first PI-Point `ConfigString` found
(one element listing + at most DISCOVER_SAMPLE_ELEMENTS attribute lookups per
database, sequential); the first exposed DA server is only a fallback. One
root endpoint failing (e.g. `/dataservers` blocked) only drops its rows.

**Concatenated ("stack by asset") extraction** (`pi_extract_assets`):
attributes grouped by parent element; output is `TS, TS_UTC, [EventFrame],
Level 1..Level K, Asset, <one column per attribute NAME>` — rows concatenated
per asset (JMP Tables > Concatenate semantics: timestamps repeat once per
asset), missing attributes become missing values. The `Level i` columns hold
the element hierarchy below the database (multi-level parent/child); `Asset`
repeats the leaf element name. This gives Seeq-style asset swapping through a
simple row filter instead of one column per asset-attribute pair.

**Event frames** (`search_event_frames` + `apply_event_frames`): frames
overlapping the extraction window can be searched by name/template; the JSL
Filters tab lets users restrict the extraction to the selected frames' time
windows — matching rows keep an `EventFrame` label column. v4.1.5: scoped to
`AF_Database` when the server-list entry sets one (like the attribute
search); all databases otherwise.

## Contracts with the JSL side (do not break these)

- `search_tags()` returns a DataFrame with columns **exactly**
  `tagnames, descriptions, units, type` (+ extra `path`) — same shape as the
  legacy SQL search, so the GUI list-box code is untouched.
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
