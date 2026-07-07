# CLAUDE.md

## What this is

A JMP add-in ("MES Data Retrieval") written in JSL (JMP Scripting Language) that extracts
time-series process data from manufacturing historians: **Aspentech InfoPlus.21 (IP21)**
and **OSIsoft/Aveva PI**. Users pick a server, search tags, set a time range and
extraction method (Interpolated / Average / Actual), optionally add filters, and get the
data as a JMP table.

**v4.1 — PI Asset Framework** (on top of the v3.0 REST architecture below):

- EXPLICIT AF mode: the "PI AF search (attributes)" checkbox (`chkbox_AFSearch` →
  `f_AF_SetMode` → `int.AFDisplayMode`) switches search + panels. Checked: attribute-only
  search (`pi_search(..., af_only=1)`, AF errors surface — no silent DA fallback),
  restricted to the server list's `AF_Database` when set (new optional column; wrong name
  raises listing the available databases). Unchecked: plain DA search like v3.
- TREE selection UI (`Ressources_REST.jsl` AF section): results tree (`tree_af` in
  `afTreeOutline`, root = database, leaves `attr {desc} [units] {type}`, tooltip = path)
  and a mirrored selection tree (`tree_sel`). Both multiselect; selecting an element
  means all its attributes (`f_AF_CollectLeafData` — recursive, needs `{Default Local}`).
  The flat lists stay the DATA MODEL (path-suffixed labels = unique identities in
  `TagSelect_AA`; pretty labels in `AA.AF_PrettyByFlat`) and are hidden in AF mode.
  The search-bar filter rebuilds the tree (`f_AF_ShowResults` + `f_MatchSearchPattern`),
  pruning elements with no matching attribute.
- Server-list discovery: "Load servers from Web API..." button → `f_REST_DiscoverServers`
  → `pi_discover` (`GET /dataservers`, `/assetservers`, per-server databases) →
  `f_BuildServersAA` (factored out of `f_LoadServerList`; also used by the Excel path).
- "Concat results" option (`OptStackAssets` → `f_RunExtraction_PI_AF_Stacked` →
  `pi_extract_assets`): output `TS, TS_UTC, [EventFrame], Level 1..K, Asset,
  <attribute-name cols>` — Level columns = element hierarchy below the database; rows
  concatenated per asset, missing attributes = missing values. Update/Refresh and
  Add-tags are guarded off for stacked tables (not yet supported); the Add-tags window
  searches DA points only (no AF checkbox there).
- Column metadata for the organizer: wide AF columns get `tag_attribute`, `af_element`,
  `af_path` properties (stacked: `tag_attribute` on attribute columns; `addin_colname`
  on `Level i`/`Asset`).
- Event frames (Filters tab): `f_REST_FindEventFrames` searches by name/template;
  selected frames' windows restrict the extraction (`EFWindows_JSON` →
  `apply_event_frames`, adds an `EventFrame` column).

**v3.0 transport architecture** (REST is the ONLY transport — no OLEDB/ODBC anywhere):

- **PI** → PI Web API (`https://<host>/piwebapi`), implemented in the Python package
  `include/Ressources/python/mes_connector/` and called from JSL via JMP 19's embedded
  Python (`include/Ressources/Ressources_REST.jsl` is the bridge). Simple filters are
  pushed server-side as PI `filterExpression`; `Like`/`Not Like`/`In` are applied in pandas.
- **IP21** → the *unchanged* proven SQLplus queries (`Ressources_SQL.jsl`) are POSTed to
  Aspen's Process Data REST service (`http://<host>/ProcessData/AtProcessDataREST.dll/SQL`).
  Filters stay fully server-side (they're in the SQL). The base URL must point at that
  .dll — `normalize_base_url` completes a bare host automatically.
- No drivers, no PowerShell: Windows and macOS run the exact same code path. A server
  without `WebAPI_URL` errors out (`f_REST_CheckConfigured`).
- The server list (`MES_servers_list.xlsx`) was simplified in v3.0 to: `site` (optional
  display name), `server` (mandatory — PI DA name / IP21 data source), `Type` (PI|IP21),
  `WebAPI_URL` (mandatory; scheme/trailing-slash tolerant via
  `mes_connector.normalize_base_url`), `PI_AF_Server` (optional; reserved for the planned
  AF attribute search with a collapsible element tree). Same fields editable in the GUI
  ("Edit server address" panel; Extension/Shortname boxes are orphan legacy widgets kept
  for report/recall compatibility).
- Debugging: the Python layer prints every request URL into the JMP log; PI extraction
  runs chunked (5 tags per call) to drive the legacy `progress:` bar.
- Auth: SSO first (SSPI/Kerberos), then OS-vault credentials (Windows Credential
  Manager / macOS Keychain via `keyring`; "Remember on this computer" in the login
  dialog), then the JSL login dialog. Basic auth refused over plain http; TLS verified
  by default via the OS trust store (`int.TLSVerify = 0` opts out for self-signed
  certs). Details in `mes_connector/auth.py`.

Read `include/Ressources/python/mes_connector/README.md` before touching the Python layer —
it documents the JSL↔Python contracts (column names the GUI depends on).

For deeper background, read the HTML guides in `doc/` (rewritten for v3.0; screenshots
extracted from the old PDFs live in `doc/img/`):

- `doc/MES Data retrieval - User Guide.html` — end-user walkthrough of the UI
- `doc/MES Data retrieval - Administrator Guide.html` — server list, security, code architecture, deployment

## Layout

- `LAUNCH_APPLICATION.jsl` — entry point: loads config + dependencies, builds the whole UI (`New Window`)
- `config/config.jsl` — version, paths, server-list location (`strPathServerlist`)
- `include/` — dependency scripts included at launch: `EXPR_DATA_EXTRACTION.jsl` (the actual
  IP21/PI queries), `EXPR_ADD_FILTER.jsl`, `EXPR_PREVIEW.jsl`, `ProgressBar.jsl`, helper functions
- `MES_servers_list.xlsx` — server registry (zone, site, DirectoryHost, extension, Port, Type IP21|PI,
  ShortName); downloaded at launch from this repo's raw GitHub URL. The old `MES_servers_list.csv`
  is the legacy format.
- `tests/` — ad-hoc test scripts (incl. the retired OLEDB PowerShell helper, kept for reference)
- `deployment/` — built `.jmpaddin` packages
- `column_organizer/` — companion add-in
- `python/`, `tests/` — PI Web API notebook experiments and ad-hoc test scripts

## Conventions & gotchas

- JSL: message-send style `obj << Message(...)`; scripts start with `Names Default To Here(1)`.
- Must run on both Windows and macOS: guard Windows-only preferences with
  `Host is( "Windows" )` (e.g. `Use JMP Locale Settings` — see LAUNCH_APPLICATION.jsl).
  Since v3.0 the transports are pure REST, so no platform-specific extraction code remains.
- `strPathServerlist` is set in `config/config.jsl` but then overridden in
  `LAUNCH_APPLICATION.jsl` (SERVER LIST section) — change both. The URL is branch-pinned
  (currently `version-2.3`); point it back to `main` when merging.
- Errors surface through `f_DialogError` / `f_DialogWarning` / `f_Log` (defined in `include/`).
- Requires JMP v16+, historian client drivers, and enterprise network/VPN access — extraction
  cannot be tested outside that environment; syntax-check JSL changes in JMP instead.
