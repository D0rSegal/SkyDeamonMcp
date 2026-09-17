# SkyDemon MCP OAuth gateway (Cloudflare Worker)

JWT-gated OAuth + reverse proxy in front of the Python MCP server, so
third-party clients (e.g. Gemini custom connected apps) can authenticate
with a standard **Client ID + Client secret**.

## Architecture

```
Gemini --Bearer JWT--> flights.segal.to/mcp  (Worker: verify, then proxy)
                         + service-token headers
                         v
                     origin.segal.to/mcp     (tunnel, Access: service token only)
                         v
                     127.0.0.1:8000          (Python, localhost-bound)
```

Public, unauthenticated: `/.well-known/*`, `/oauth/token`,
`/oauth/authorize`, `/oauth/register`. Everything under `/mcp` needs a
Bearer JWT (401 + `WWW-Authenticate: resource_metadata=...` otherwise,
per MCP OAuth discovery).

## One-time setup

Prereqs: zone `segal.to` Active on Cloudflare, tunnel `skydemon-mcp`
running, python server on `--transport streamable-http --port 8000`.

```powershell
# 0. DNS for the origin hostname (tunnel already exists)
& "C:\Program Files (x86)\cloudflared\cloudflared.exe" tunnel route dns skydemon-mcp origin.segal.to

# 1. Lock the origin: Cloudflare dashboard -> Zero Trust -> Access ->
#    Service Auth -> create token (save its ID + secret) -> Applications ->
#    Add self-hosted app for origin.segal.to -> policy Allow, include the
#    service token only.

# 2. Deploy the worker (needs node + wrangler, browser login once)
npm i -g wrangler
wrangler login
wrangler secret put OAUTH_CLIENT_ID
wrangler secret put OAUTH_CLIENT_SECRET
wrangler secret put JWT_SECRET
wrangler secret put ORIGIN_BASE        # https://origin.segal.to
wrangler secret put SF_CLIENT_ID       # Access service token ID
wrangler secret put SF_CLIENT_SECRET   # Access service token secret
```

Fill `zone_id` in `wrangler.toml` (dash.cloudflare.com -> segal.to ->
Overview -> API -> Zone ID), uncomment the three `[[routes]]`, then:

```powershell
wrangler deploy
```

## Gemini custom connected app values

- App link: `https://flights.segal.to/mcp`
- Client ID: `sdmc-5d1bf4b456d49c66`
- Client secret: (generated at setup, stored as worker secret — see deploy log)

## Test

```powershell
npm test   # 13 checks, no network
curl -X POST https://flights.segal.to/oauth/token `
  -d "grant_type=client_credentials&client_id=sdmc-...&client_secret=..."
# -> {"access_token":"...","token_type":"Bearer",...}
```

## Notes

- Tokens are stateless HMAC JWTs (1h access, 30d refresh, 10min codes).
  Rotate by changing `JWT_SECRET` (invalidates everything).
- `/oauth/authorize` is a single-owner approval page; redirect URIs are
  limited to `https://*` + localhost by default (`ALLOWED_REDIRECTS`).
- Keep the Python server on `127.0.0.1` — it must never be reachable
  directly, only via the Access-locked tunnel hostname.
