# SkyDeamonMcp

MCP wrapper for SkyDemon (Windows). Reverse-engineered login interop, read-only
flightplan access, and offline airfield search + pilot notes / live feedback.
No SkyDemon code is shipped — only wire/file formats are reimplemented
(see `tmp/decompiled/`, git-ignored).

## Setup

```powershell
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
# or: .\.venv\Scripts\python.exe -m pip install -e . pytest
```

## Credentials

Copy `.env.example` to `.env` (git-ignored, never commit it):

```powershell
Copy-Item .env.example .env
```

```
SKYDEMON_LOGIN=you@example.com
SKYDEMON_PASSWORD=...
```

Optional: `SKYDEMON_ROUTES_DIR` to override `~/Documents/SkyDemon/Routes`,
`SKYDEMON_CHARTS_DIR` for charts, `SKYDEMON_INSTALL_DIR` for the install dir.

Device identity is auto-detected per machine like the app does
(`MachineGuid` → fastest MAC → fallback hash; WMI manufacturer/model),
so moving machines just works — no const IDs.

Requires `mcp<2` (v1 FastMCP API).

## Test the API (live login, redacted output)

```powershell
.\.venv\Scripts\python.exe tmp\live_check.py
.\.venv\Scripts\python.exe -m pytest tests -q
```

`live_check.py` loads `.env` in-process and prints only the license summary
(name, type, expiry, token tail) — never the password or full token.

## Run the MCP server (stdio)

```powershell
$env:SKYDEMON_LOGIN="you@example.com"; $env:SKYDEMON_PASSWORD="..."
.\.venv\Scripts\skydeamon-mcp
# or: .\.venv\Scripts\python.exe -m skydeamon.server
```

It logs in once on startup and holds the session in memory (`skydeamon/session.py`).
Claude Desktop config:

```json
{
  "mcpServers": {
    "skydemon": {
      "command": "C:\\Git\\SkyDeamonMcp\\.venv\\Scripts\\skydeamon-mcp.exe",
      "env": { "SKYDEMON_LOGIN": "...", "SKYDEMON_PASSWORD": "..." }
    }
  }
}
```

## Tools

| Tool | What it does |
|---|---|
| `skydemon_login` | Login once, reuse cached session (`force:true` to refresh) |
| `skydemon_session_status` | Cached session state (token tail only) |
| `skydemon_logout` | Drop the in-memory session |
| `skydemon_api_info` | API base + client identity |
| `skydemon_list_flightplans` | List `.flightplan`/`.gpx` on disk (read-only) |
| `skydemon_read_flightplan` | Summarize one plan (routes, legs, aircraft) |
| `skydemon_search_airfields` | General search over installed charts (ICAO/name, offline) |
| `skydemon_airfield_info` | Full record: runways, frequencies, fuel, circuits, contacts + pilot notes & live feedback (online, needs login) |
| `skydemon_list_cloud_flightplans` | List flightplans in cloud storage (read-only) |
| `skydemon_download_cloud_flightplan` | Download + summarize one cloud plan (`save:true` keeps a local copy) |

## Layout

- `skydeamon/config.py` — server URLs, GUIDs (from ILSpy decompile)
- `skydeamon/api.py` — `Login/LoginDevice` client + `DeviceLogin` parser
- `skydeamon/session.py` — in-memory session cache
- `skydeamon/flightplans.py` — read-only disk parsing
- `skydeamon/airfields.py` — offline chart index (search) + pilot notes / feedback
- `skydeamon/server.py` — MCP server
- `tmp/` — local-only decompile + scratch (ignored)
