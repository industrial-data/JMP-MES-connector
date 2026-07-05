# MES data retrieval add-in — Aspentech IP.21 and OSIsoft PI (Aveva)

Aspentech IP.21 and OSIsoft/Aveva PI connector to JMP (SAS Institute). You can find a walk-through demo [here](https://community.jmp.com/t5/Discovery-Summit-Europe-2023/Scaling-up-the-Use-of-Machine-Learning-in-Chemical-Process/ta-p/572644) (add-in shown at minute 21).

:inbox_tray: [Download latest version](https://github.com/industrial-data/JMP-MES-connector/raw/main/IP21PI_Data_Retrieval_v_latest.jmpaddin)

:inbox_tray: [+ Column organizer](https://github.com/industrial-data/JMP-MES-connector/raw/main/column_organizer/Column%20organizer%20v220905.jmpaddin)

This add-in automates data extraction from Aspentech IP.21 and OSIsoft PI (Aveva) historians, so you can use JMP to diagnose manufacturing problems and monitor tags daily and weekly.

Have a look at this [review](https://pubs.rsc.org/en/content/articlelanding/2022/re/d1re00541c) for more industrial data science applications. If you would like JMP to support historians natively, give it a thumbs up in the [wishlist](https://community.jmp.com/t5/JMP-Wish-List/Native-Support-to-Manufacturing-Historians-Aspentech-IP-21-and/idi-p/540846). Similar work: [JMP OsiPITools](https://github.com/himanga/JMPOSIPITools), [Tagreader Python library](https://github.com/equinor/tagreader-python).

## What's new in v4.0 — Asset Framework edition

v4.0 makes the add-in **asset-centric** (inspired by Seeq's asset workflows):

- **AF attribute search**: with a `PI_AF_Server` configured, the search bar finds every
  Asset Framework **attribute** matching the name/description — results are shown both as
  a **collapsible element hierarchy** (parent/child tree) and in the flat selection list,
  labeled `attribute (description) [units] {AF path}`. Tags not in AF (plain DA points)
  can be listed too via an option under the results.
- **Stack by asset**: when the selection spans sibling assets (e.g. the `Temperature` of
  reactors A, B and C), a new option extracts *per asset, concatenated* — **one column per
  attribute name plus an `Asset` column**, timestamps repeated per asset (JMP
  Tables > Concatenate semantics). An asset missing an attribute simply gets missing
  values. Switch the analyzed asset with a local data filter on `Asset` instead of
  rebuilding your analysis.
- **Event frames**: the Filters tab can search the server's PI Event Frames (by name
  and/or template) and restrict the extraction to the selected frames' time windows;
  extracted rows carry an `EventFrame` label column.

## What's new in v3.0 — REST edition

Version 3.0 is a major rework: **all extraction now goes through the historians' REST web
services**. The OLEDB / ODBC / PowerShell code paths were removed entirely.

- **PI** → native [PI Web API](https://docs.aveva.com/bundle/pi-web-api-reference/page/help.html) (search, interpolated / recorded / summary streams).
- **IP21** → the proven SQLplus queries, unchanged, wrapped in POSTs to Aspen's Process
  Data REST service (`http://<host>/ProcessData/AtProcessDataREST.dll/SQL`) — roughly 5×
  faster than the old ODBC round-trips, with filters still executed server-side.
- **No drivers to install.** Windows and macOS run the identical code path.
- New **Step Interpolated** extraction method (staircase / previous-value-held), on top of
  Interpolated, Average and Actual.
- **Uncapped tag search** (pages through the whole point database).
- **Secure logins**: Windows SSO / Kerberos first; otherwise a login dialog that can
  remember credentials in the **Windows Credential Manager / macOS Keychain** (encrypted
  by the OS, never on disk). Passwords are refused over unencrypted HTTP, and TLS
  certificates are verified by default.
- Every REST request URL is echoed in the JMP log for easy debugging.
- Update/Refresh prefills with the table's real data window (oldest timestamp → now) and
  replaces overlapping rows instead of duplicating them.

The REST calls are made by a small embedded Python package
([`include/Ressources/python/mes_connector`](include/Ressources/python/mes_connector/README.md))
driven from JSL through JMP's embedded Python.

## Documentation

- :blue_book: [User Guide](doc/MES%20Data%20retrieval%20-%20User%20Guide.html) — installation, GUI, filters, additional scripts
- :wrench: [Administrator Guide](doc/MES%20Data%20retrieval%20-%20Administrator%20Guide.html) — server list, security, code architecture, deployment

## Requirements

- **JMP 18 or higher** (the add-in uses JMP's embedded Python).
- Network access (enterprise network / VPN) to the historian's REST endpoint:
  - PI Web API, e.g. `https://piserver.company.com/piwebapi`
  - Aspen Process Data REST, e.g. `http://ip21server.company.com/ProcessData/AtProcessDataREST.dll`
- Read permissions on the historian (your AD account or a provided login).
- One-time internet/proxy access on first run (the add-in installs the `requests` Python
  package into JMP's Python environment automatically).

No Aspen ODBC driver, no PI OLEDB, no PI client tools — those were v2.x requirements.

## Functionalities

The **interface** allows users to:

1. Select a server from the server list (shown as `site [type] - server`) or enter the
   details manually (*Edit server address*), and name the extracted table
2. Find tags by tag name and/or description (uncapped search)
3. Add selected tags, or paste tag names copied from a spreadsheet
4. Filter by start and end date (now and 24 h earlier by default)
5. Select the extraction method — interpolated, average, actual, or **step interpolated**
6. Select the extraction period (10 minutes by default)
7. Add value filters on tags (with AND/OR combinations, nesting and preview)

Server details needed in v3.0 (see the server list section below):

| Field | Required | Example |
|---|---|---|
| Server name (PI DA / IP21 data source) | yes | `PIDASERVER` / `ES-BCN-S01` |
| WebAPI URL | yes | `piserver.company.com/piwebapi` (scheme/slash optional) |
| PI AF server | no | `NY-AF01` (future attribute search) |

The extraction runs in parallel batches with a progress bar and a high row limit per tag
(1,000,000) to not saturate the server. Please notify your OT/IT team in advance if you
plan massive data extractions.

After extraction you can **UPDATE**, **REFRESH**, and **ADD tags** to the table with one
click. The UTC timestamp (`TS_UTC` column) is used by these scripts, so keep it in your
table. If you want to rename tag columns, create new columns with formulas referring to
them instead.

## What's the difference between update() and refresh()?

Both automatically obtain recent data from the MES.

- **UPDATE** completes the table: new rows are added, and if the requested window overlaps
  existing rows, those are replaced by fresh data (no duplicated timestamps).
- **REFRESH** re-extracts and replaces the whole table content (the old rows are only
  removed after the new extraction succeeded).

Use update() to keep track of all new and old data.

## How can I organize all the tags (columns) after the extraction?

You can easily group and ungroup the tags with our Column Organizer add-in —
[Download](https://github.com/industrial-data/JMP-MES-connector/raw/main/column_organizer/Column%20organizer%20v220905.jmpaddin)

## Server list

The servers offered in the GUI come from [`MES_servers_list.xlsx`](MES_servers_list.xlsx)
(sheet `MES_servers_list`), downloaded at launch from the URL configured in
`config/config.jsl`. Columns:

| Column | Required | Content |
|---|---|---|
| `site` | no | Display name shown in the GUI list; falls back to the server name when empty. Not used by the extraction. |
| `server` | **yes** | PI Data Archive name or IP21 ADSA data source name |
| `Type` | **yes** | `PI` or `IP21` |
| `WebAPI_URL` | **yes** | REST endpoint (tolerant to missing `https://` and trailing `/`) |
| `PI_AF_Server` | no | AF server for the future attribute search |

To publish your own list, host the Excel somewhere reachable (e.g. a Git repository) and
point `strPathServerlist` to it.

## Security

- SSO (SSPI/Kerberos) is tried first; explicit logins can be remembered in the OS
  credential vault (Windows Credential Manager / macOS Keychain).
- Passwords are never written to files or logs, never sent over plain `http://`, and TLS
  certificates are verified by default (`int.TLSVerify = 0` in `config/config.jsl` is the
  logged opt-out for self-signed plant certificates).

## Creating your add-in

You can modify the source code and GUI at will. Keep the add-in ID
(`MES.Data.Retrieval.OS2.2`) unchanged so existing installations upgrade in place. For
development, `LAUNCH_APPLICATION.jsl` can be run directly from the repository without
deploying a `.jmpaddin` — paths resolve from the script's folder. Package by zipping the
repository content with the add-in definition (see `deployment/`), making sure the
`include/Ressources/python/mes_connector` folder is included.

## Troubleshooting

- Check the JMP log (Ctrl+Shift+L): **every REST request URL and status is echoed
  there**, plus a `Python init` line confirming the Python layer loaded.
- *"No WebAPI URL configured"* — add the URL in the server list or in *Edit server
  address*.
- SSL/certificate errors — install the corporate CA, or set `int.TLSVerify = 0` for
  self-signed plant servers.
- Aspen errors are returned in the REST payload and shown verbatim in the log.

## License

This add-in is open-source (BSD clause 3).

## Roadmap

- Update/Refresh and Add-tags support for asset-stacked tables.
- Tree-side selection (click attributes directly in the hierarchy).
- More extraction options (additional summary types, event-frame attributes).

Suggestions, issues, and pull requests are more than welcome.
