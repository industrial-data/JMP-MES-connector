# CLAUDE.md

## What this is

A JMP add-in ("MES Data Retrieval") written in JSL (JMP Scripting Language) that extracts
time-series process data from manufacturing historians: **Aspentech InfoPlus.21 (IP21)**
and **OSIsoft/Aveva PI**. Users pick a server, search tags, set a time range and
extraction method (Interpolated / Average / Actual), optionally add filters, and get the
data as a JMP table.

**v3.0 transport architecture** (REST by default, drivers as fallback):

- **PI** → PI Web API (`https://<host>/piwebapi`), implemented in the Python package
  `include/Ressources/python/mes_connector/` and called from JSL via JMP 19's embedded
  Python (`include/Ressources/Ressources_REST.jsl` is the bridge). Simple filters are
  pushed server-side as PI `filterExpression`; `Like`/`Not Like`/`In` are applied in pandas.
- **IP21** → the *unchanged* legacy SQLplus queries (`Ressources_SQL.jsl`) are POSTed to
  Aspen's Process Data REST service (`http://<host>/ProcessData/AtProcessDataREST.dll/SQL`)
  instead of going through ODBC. Filters stay fully server-side (they're in the SQL).
- **Fallback**: the v2.x OLEDB (PI, via PowerShell) / ODBC (IP21) path still exists and is
  used when REST fails or no `WebAPI_URL` is configured — **Windows only**.
- The server list (`MES_servers_list.xlsx`) has two v3.0 columns: `WebAPI_URL` and
  `DAServer` (PI Data Archive name / IP21 ADSA data source name). Also editable in the GUI
  ("Edit server address" panel). `config.jsl` has `int.UseREST = 1` to force legacy mode.
- Auth: SSO first (SSPI/Kerberos), JSL login dialog on 401 (see `mes_connector/auth.py`).

Read `include/Ressources/python/mes_connector/README.md` before touching the Python layer —
it documents the JSL↔Python contracts (column names the GUI depends on).

For deeper background, read the PDFs in `doc/`:

- `doc/MES Data retrieval - User Guide - OS.pdf` — end-user walkthrough of the UI
- `doc/MES Data retrieval - Administrator guide - OS.pdf` — setup, drivers, server list administration

## Layout

- `LAUNCH_APPLICATION.jsl` — entry point: loads config + dependencies, builds the whole UI (`New Window`)
- `config/config.jsl` — version, paths, server-list location (`strPathServerlist`)
- `include/` — dependency scripts included at launch: `EXPR_DATA_EXTRACTION.jsl` (the actual
  IP21/PI queries), `EXPR_ADD_FILTER.jsl`, `EXPR_PREVIEW.jsl`, `ProgressBar.jsl`, helper functions
- `MES_servers_list.xlsx` — server registry (zone, site, DirectoryHost, extension, Port, Type IP21|PI,
  ShortName); downloaded at launch from this repo's raw GitHub URL. The old `MES_servers_list.csv`
  is the legacy format.
- `doc/external/OLEDB_extract.ps1` — PowerShell helper for PI OLEDB extraction (Windows-only)
- `deployment/` — built `.jmpaddin` packages
- `column_organizer/` — companion add-in
- `python/`, `tests/` — PI Web API notebook experiments and ad-hoc test scripts

## Conventions & gotchas

- JSL: message-send style `obj << Message(...)`; scripts start with `Names Default To Here(1)`.
- Must run on both Windows and macOS: guard Windows-only preferences/features with
  `Host is( "Windows" )` (e.g. `Use JMP Locale Settings` — see LAUNCH_APPLICATION.jsl).
  The OLEDB/PowerShell path is Windows-only.
- `strPathServerlist` is set in `config/config.jsl` but then overridden in
  `LAUNCH_APPLICATION.jsl` (SERVER LIST section) — change both. The URL is branch-pinned
  (currently `version-2.3`); point it back to `main` when merging.
- Errors surface through `f_DialogError` / `f_DialogWarning` / `f_Log` (defined in `include/`).
- Requires JMP v16+, historian client drivers, and enterprise network/VPN access — extraction
  cannot be tested outside that environment; syntax-check JSL changes in JMP instead.
