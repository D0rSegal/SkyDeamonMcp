# SkyDeamonMcp

MCP wrapper for SkyDemon (Windows). Reverse-engineered login interop, read-only
flightplan access, offline airfield search + pilot notes / live feedback, and
METAR/TAF weather. No SkyDemon code is shipped — only wire/file formats are
reimplemented (see `tmp/decompiled/`, git-ignored).

Works locally over stdio (Claude Desktop) or Streamable HTTP (localhost), and
remotely behind a tunnel with a built-in OAuth 2.0 provider for Gemini.

## Setup

```powershell
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
# or: .\.venv\Scripts\python.exe -m pip install -e . pytest
```

Requires `mcp<2` (v1 FastMCP API).

## Credentials

Copy `.env.example` to `.env` (git-ignored, never commit it):

```powershell
Copy-Item .env.example .env
```

| Variable | Needed for | Notes |
|---|---|---|
| `SKYDEMON_LOGIN` / `SKYDEMON_PASSWORD` | Everything online | Server logs in once at startup, holds session in memory. Real env wins over `.env`. |
| `MCP_OAUTH_CLIENT_ID` / `MCP_OAUTH_CLIENT_SECRET` | Remote (tunnel) access | Bearer/master credentials for the built-in OAuth provider. Not needed for localhost use. |
| `MCP_OAUTH_REDIRECT_URI` | Gemini connect flow | Your Gemini redirect URI (comma-separated list also ok via `MCP_ALLOWED_REDIRECT_URIS`). |
| `SKYDEMON_ROUTES_DIR` | Local plans | Override for `~/Documents/SkyDemon/Routes`. |
| `SKYDEMON_CHARTS_DIR` | Airfield index | Override for chart lookup. |
| `SKYDEMON_INSTALL_DIR` | Airfield index | Override for SkyDemon install dir. |
| `SKYDEMON_ALLOWED_HOSTS` | Tunnel/proxy | Extra `Host` values allowed through DNS-rebinding protection. |

Device identity is auto-detected per machine like the app does
(`MachineGuid` → fastest MAC → fallback hash; WMI manufacturer/model),
so moving machines just works — no const IDs.

## Run the MCP server

```powershell
# stdio (Claude Desktop)
.\.venv\Scripts\python.exe -m skydeamon.server
# or: .\.venv\Scripts\skydeamon-mcp.exe

# HTTP on localhost (no OAuth needed)
.\.venv\Scripts\skydeamon-mcp --transport streamable-http --port 8000
# flags: --transport stdio|streamable-http|sse (default stdio), --host, --port
```

Serves the same tools at `http://127.0.0.1:8000/mcp` (verified: initialize →
`tools/list` → `tools/call`). Keep localhost use on localhost — the endpoint
has no auth of its own; only expose it via the OAuth-guarded setup below.

Claude Desktop config (stdio):

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

## Remote access + OAuth 2.0 (Gemini)

When the server sits behind a tunnel/proxy (e.g. Cloudflare Tunnel →
`https://flights.segal.to`), the MCP endpoint is guarded by a small built-in
OAuth 2.0 provider (`skydeamon/auth.py`) plus bearer-checking middleware.
Localhost traffic is unaffected.

### Endpoints

| Route | Spec | Public? |
|---|---|---|
| `/.well-known/oauth-authorization-server` | RFC 8414 authorization server metadata | Yes |
| `/.well-known/openid-configuration` | OIDC discovery (same document) | Yes |
| `/.well-known/oauth-protected-resource` (+ `/mcp` suffix) | RFC 9728 protected-resource metadata | Yes |
| `/oauth/register` | RFC 7591 dynamic client registration | Yes |
| `/oauth/authorize` (GET + POST) | RFC 6749 authorize, renders an Approve/Deny page | Yes |
| `/oauth/token` | Grants: `authorization_code` (PKCE S256/plain), `client_credentials`, `refresh_token` | Yes |
| `/mcp` (and `/sse`, `/messages`) | Bearer-guarded MCP | No — needs `Authorization: Bearer <token>` |

Unauthenticated MCP calls get `401` with a
`WWW-Authenticate: Bearer resource_metadata=".../.well-known/oauth-protected-resource"`
header so clients can discover auth automatically.

### How it works

1. Gemini discovers `/.well-known/oauth-protected-resource` → authorization server metadata.
2. It registers (or uses the configured client id) and opens `/oauth/authorize`.
3. You click **Approve & Connect** → 302 redirect back with a one-time `code` (10-min TTL, PKCE-verified).
4. Gemini exchanges the code at `/oauth/token` for a Bearer `access_token` (30 days) + `refresh_token` (60 days). Tokens live only in server memory.
5. Every `/mcp` call must carry `Authorization: Bearer <token>`. The master `MCP_OAUTH_CLIENT_SECRET` is also accepted directly as a Bearer token (handy for testing with curl).

Google's redirect URI must be allow-listed: set `MCP_OAUTH_REDIRECT_URI` in
`.env` (see `.env.example`). Google user-bound redirects
(`https://oauth-redirect.googleusercontent.com/...`), `gemini.google.com`,
and localhost are accepted by default; anything else is rejected with 400.

### Test the API (live login, redacted output)

```powershell
.\.venv\Scripts\python.exe tmp\live_check.py
.\.venv\Scripts\python.exe -m pytest tests -q
```

`live_check.py` loads `.env` in-process and prints only the license summary
(name, type, expiry, token tail) — never the password or full token.

## Tools

All read-only (no permission prompts) except `skydemon_download_cloud_flightplan`
with `save:true`, plus `login`/`logout` which only touch the in-memory session.

| Tool | What it does |
|---|---|
| `skydemon_login` | Rarely needed (auto-login on startup). Switch user / force refresh / validate creds. Returns token **tail** only. |
| `skydemon_session_status` | Cached session state (token tail only). Check before online tools. |
| `skydemon_logout` | Drop the in-memory session. Next online call re-logs in transparently. |
| `skydemon_api_info` | Diagnostics: API base + client identity. |
| `skydemon_list_flightplans` | List local `.flightplan`/`.gpx` on disk (offline, no login). |
| `skydemon_read_flightplan` | Summarize one local plan (routes, legs, aircraft). |
| `skydemon_search_airfields` | Entry point for airfields: ICAO/name over offline charts (no login). Always start here. |
| `skydemon_airfield_info` | Full record for ONE field: runways, frequencies, fuel, circuits, contacts + pilot notes & live feedback (online, needs login). |
| `skydemon_airfields_info` | Full records for 2–20 fields in one throttled, offline-cached batch call. Prefer over N single calls. |
| `skydemon_list_cloud_flightplans` | List flightplans in cloud storage (online, needs login). |
| `skydemon_download_cloud_flightplan` | Download + summarize one cloud plan (`save:true` keeps a local copy; only writer). |
| `skydemon_airfield_weather` | Current METAR + TAF for an airfield (`what: metar\|taf\|both`, raw bulletins, 5-min cache). |

## Layout

- `skydeamon/config.py` — server URLs, GUIDs (from ILSpy decompile)
- `skydeamon/api.py` — `Login/LoginDevice` client + `DeviceLogin` parser
- `skydeamon/session.py` — in-memory session cache
- `skydeamon/auth.py` — OAuth 2.0 provider + bearer middleware (Gemini remote access)
- `skydeamon/flightplans.py` — read-only disk parsing
- `skydeamon/airfields.py` — offline chart index (search) + pilot notes / feedback
- `skydeamon/weather.py` — METAR/TAF via Bulletin/Refresh (5-min cache)
- `skydeamon/cloud.py` — cloud list/download (no upload/delete)
- `skydeamon/server.py` — MCP server (stdio + `--transport streamable-http`)
- `skydeamon/logging_config.py` — request/tool-call logging
- `tmp/` — local-only decompile + scratch (ignored)
