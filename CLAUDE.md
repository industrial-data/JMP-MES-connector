# CLAUDE.md

## What this is

A JMP add-in ("MES Data Retrieval") written in JSL (JMP Scripting Language) that extracts
time-series process data from manufacturing historians: **Aspentech InfoPlus.21 (IP21)**
via SQLplus ODBC, and **OSIsoft/Aveva PI** via the PI OLEDB Enterprise driver. Users pick
a server, search tags, set a time range and extraction method (Interpolated / Average /
Actual), optionally add filters, and get the data as a JMP table.

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
