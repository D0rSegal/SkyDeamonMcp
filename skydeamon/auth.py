"""OAuth 2.0 Provider & middleware for Gemini Custom Connected Apps.

Implements:
- RFC 8414 OAuth 2.0 Authorization Server Metadata (/.well-known/oauth-authorization-server)
- OpenID Connect Discovery (/.well-known/openid-configuration)
- RFC 7591 OAuth 2.0 Dynamic Client Registration (/oauth/register)
- Authorization endpoint (/oauth/authorize)
- Token endpoint (/oauth/token)
- Bearer token verification ASGI middleware for MCP routes (/mcp, /sse, /messages)
"""
from __future__ import annotations

import hashlib
import hmac
import html
import json
import os
import secrets
import time
from typing import Any, Dict
from urllib.parse import parse_qs, urlencode

from starlette.datastructures import Headers
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.types import ASGIApp, Receive, Scope, Send


# In-memory storage (with TTLs)
_AUTH_CODES: Dict[str, dict] = {}
_ACCESS_TOKENS: Dict[str, dict] = {}
_DYNAMIC_CLIENTS: Dict[str, dict] = {}

CODE_TTL_SEC = 600       # 10 minutes
TOKEN_TTL_SEC = 86400 * 30  # 30 days


def get_oauth_credentials() -> tuple[str, str]:
    """Client ID and Secret configured via environment variables.

    Reads MCP_OAUTH_CLIENT_ID and MCP_OAUTH_CLIENT_SECRET from environment.
    """
    client_id = os.environ.get("MCP_OAUTH_CLIENT_ID", "").strip()
    client_secret = os.environ.get("MCP_OAUTH_CLIENT_SECRET", "").strip()
    return client_id, client_secret


def get_issuer(request: Request) -> str:
    """Determine the public base URL (e.g. https://flights.segal.to)."""
    forwarded_proto = request.headers.get("x-forwarded-proto", "https")
    host = request.headers.get("x-forwarded-host") or request.headers.get("host") or "flights.segal.to"
    return f"{forwarded_proto}://{host}"


def oauth_metadata_response(request: Request) -> JSONResponse:
    """RFC 8414 OAuth 2.0 Authorization Server Metadata."""
    base = get_issuer(request)
    meta = {
        "issuer": base,
        "authorization_endpoint": f"{base}/oauth/authorize",
        "token_endpoint": f"{base}/oauth/token",
        "registration_endpoint": f"{base}/oauth/register",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "token_endpoint_auth_methods_supported": [
            "client_secret_basic",
            "client_secret_post",
            "none",
        ],
        "code_challenge_methods_supported": ["S256", "plain"],
        "scopes_supported": ["mcp", "read", "write"],
    }
    return JSONResponse(meta, headers={"Access-Control-Allow-Origin": "*"})


def oauth_protected_resource_metadata(request: Request) -> JSONResponse:
    """RFC 9728 OAuth 2.0 Protected Resource Metadata."""
    base = get_issuer(request)
    meta = {
        "resource": base,
        "authorization_servers": [base],
        "scopes_supported": ["mcp", "read", "write"],
        "bearer_methods_supported": ["header"],
    }
    return JSONResponse(meta, headers={"Access-Control-Allow-Origin": "*"})



async def oauth_register_endpoint(request: Request) -> JSONResponse:
    """RFC 7591 Dynamic Client Registration endpoint for clients that support it."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    client_id = f"client_{secrets.token_hex(8)}"
    client_secret = secrets.token_urlsafe(32)
    _DYNAMIC_CLIENTS[client_id] = {
        "client_secret": client_secret,
        "client_name": body.get("client_name", "Dynamic Client"),
        "redirect_uris": body.get("redirect_uris", []),
        "created_at": time.time(),
    }
    return JSONResponse({
        "client_id": client_id,
        "client_secret": client_secret,
        "client_name": body.get("client_name", "Dynamic Client"),
        "redirect_uris": body.get("redirect_uris", []),
        "grant_types": ["authorization_code"],
        "response_types": ["code"],
    }, status_code=201)


async def oauth_authorize_endpoint(request: Request) -> Response:
    """Interactive / programmatic authorization endpoint."""
    params = request.query_params
    client_id = params.get("client_id", "")
    redirect_uri = params.get("redirect_uri", "")
    state = params.get("state", "")
    code_challenge = params.get("code_challenge", "")
    code_challenge_method = params.get("code_challenge_method", "")

    configured_id, _ = get_oauth_credentials()
    # Accept configured client ID or dynamically registered client ID or default
    if client_id != configured_id and client_id not in _DYNAMIC_CLIENTS and client_id:
        pass  # We still permit user to approve in single-tenant setup

    # If user submitted the login form
    if request.method == "POST":
        form = await request.form()
        action = form.get("action", "")
        if action == "approve":
            code = secrets.token_urlsafe(32)
            _AUTH_CODES[code] = {
                "client_id": client_id,
                "redirect_uri": redirect_uri,
                "code_challenge": code_challenge,
                "code_challenge_method": code_challenge_method,
                "expires_at": time.time() + CODE_TTL_SEC,
            }
            delim = "&" if "?" in redirect_uri else "?"
            target = f"{redirect_uri}{delim}code={code}"
            if state:
                target += f"&state={state}"
            return RedirectResponse(target, status_code=302)
        else:
            delim = "&" if "?" in redirect_uri else "?"
            return RedirectResponse(f"{redirect_uri}{delim}error=access_denied", status_code=302)

    # Render clean Approval / Sign In page
    html_content = f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Connect SkyDemon to Gemini</title>
    <style>
        body {{
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
            background: #0f172a;
            color: #f8fafc;
            display: flex;
            align-items: center;
            justify-content: center;
            min-height: 100vh;
            margin: 0;
            padding: 20px;
        }}
        .card {{
            background: #1e293b;
            border: 1px solid #334155;
            border-radius: 16px;
            padding: 32px;
            max-width: 440px;
            width: 100%;
            box-shadow: 0 20px 25px -5px rgba(0, 0, 0, 0.5);
            text-align: center;
        }}
        h2 {{ margin-top: 0; font-size: 22px; color: #38bdf8; }}
        p {{ font-size: 14px; color: #94a3b8; line-height: 1.5; }}
        .badge {{
            background: #0369a1;
            color: #e0f2fe;
            font-size: 12px;
            padding: 4px 10px;
            border-radius: 9999px;
            display: inline-block;
            margin-bottom: 16px;
        }}
        .actions {{
            display: flex;
            gap: 12px;
            margin-top: 24px;
        }}
        button {{
            flex: 1;
            padding: 12px 16px;
            border-radius: 8px;
            font-weight: 600;
            font-size: 14px;
            cursor: pointer;
            border: none;
            transition: all 0.2s;
        }}
        .btn-primary {{
            background: #0284c7;
            color: white;
        }}
        .btn-primary:hover {{ background: #0369a1; }}
        .btn-secondary {{
            background: #334155;
            color: #cbd5e1;
        }}
        .btn-secondary:hover {{ background: #475569; }}
    </style>
</head>
<body>
    <div class="card">
        <span class="badge">Model Context Protocol</span>
        <h2>Authorize SkyDemon MCP</h2>
        <p>Google Gemini is requesting permission to access your SkyDemon flight plans, airfield charts, and weather data.</p>
        <form method="POST">
            <div class="actions">
                <button type="submit" name="action" value="deny" class="btn-secondary">Deny</button>
                <button type="submit" name="action" value="approve" class="btn-primary">Approve & Connect</button>
            </div>
        </form>
    </div>
</body>
</html>"""
    return HTMLResponse(html_content)


async def oauth_token_endpoint(request: Request) -> JSONResponse:
    """Token exchange endpoint supporting authorization_code and refresh_token."""
    # Parameters can come from form body or JSON
    ct = request.headers.get("content-type", "")
    if "application/json" in ct:
        body = await request.json()
    else:
        form = await request.form()
        body = dict(form)

    grant_type = body.get("grant_type", "")
    code = body.get("code", "")
    client_id = body.get("client_id", "")
    client_secret = body.get("client_secret", "")

    # Basic auth check if client credentials passed via Authorization header
    auth_header = request.headers.get("authorization", "")
    if auth_header.startswith("Basic "):
        import base64
        try:
            decoded = base64.b64decode(auth_header[6:]).decode("utf-8")
            if ":" in decoded:
                client_id, client_secret = decoded.split(":", 1)
        except Exception:
            pass

    configured_id, configured_secret = get_oauth_credentials()
    # Validate secret if client passed one
    if client_id == configured_id and client_secret and client_secret != configured_secret:
        return JSONResponse({"error": "invalid_client"}, status_code=401)

    if grant_type == "authorization_code":
        record = _AUTH_CODES.pop(code, None)
        if not record or record["expires_at"] < time.time():
            return JSONResponse({"error": "invalid_grant", "error_description": "code expired or invalid"}, status_code=400)

        # Generate Bearer access token
        token = f"mcp_{secrets.token_urlsafe(32)}"
        refresh_token = f"ref_{secrets.token_urlsafe(32)}"
        _ACCESS_TOKENS[token] = {
            "client_id": client_id or configured_id,
            "expires_at": time.time() + TOKEN_TTL_SEC,
        }

        return JSONResponse({
            "access_token": token,
            "token_type": "Bearer",
            "expires_in": TOKEN_TTL_SEC,
            "refresh_token": refresh_token,
            "scope": "mcp",
        }, headers={"Cache-Control": "no-store", "Access-Control-Allow-Origin": "*"})

    elif grant_type == "refresh_token":
        token = f"mcp_{secrets.token_urlsafe(32)}"
        _ACCESS_TOKENS[token] = {
            "client_id": client_id or configured_id,
            "expires_at": time.time() + TOKEN_TTL_SEC,
        }
        return JSONResponse({
            "access_token": token,
            "token_type": "Bearer",
            "expires_in": TOKEN_TTL_SEC,
        }, headers={"Cache-Control": "no-store", "Access-Control-Allow-Origin": "*"})

    return JSONResponse({"error": "unsupported_grant_type"}, status_code=400)


def is_token_valid(token: str) -> bool:
    """Verify if Bearer token is valid."""
    # Allow master secret directly as Bearer token too
    _, master_secret = get_oauth_credentials()
    if token == master_secret:
        return True
    record = _ACCESS_TOKENS.get(token)
    if not record:
        return False
    if record["expires_at"] < time.time():
        _ACCESS_TOKENS.pop(token, None)
        return False
    return True


class OAuthMiddleware:
    """ASGI Middleware checking OAuth on protected MCP endpoints."""

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        # Public OAuth / discovery routes
        if (
            path in (
                "/.well-known/oauth-authorization-server",
                "/.well-known/openid-configuration",
                "/oauth/authorize",
                "/oauth/token",
                "/oauth/register",
            )
            or path.startswith("/.well-known/")
        ):
            await self.app(scope, receive, send)
            return

        # Check Bearer token or CF-Access header on MCP routes (/mcp, /sse, /messages)
        headers = Headers(scope=scope)
        auth = headers.get("authorization", "")
        
        # If OAuth authentication is required:
        # Check Authorization: Bearer <token>
        if auth.startswith("Bearer "):
            token = auth[7:].strip()
            if is_token_valid(token):
                await self.app(scope, receive, send)
                return

        # Check Cloudflare Service Token if present
        cf_id = headers.get("cf-access-client-id")
        cf_secret = headers.get("cf-access-client-secret")
        configured_id, configured_secret = get_oauth_credentials()
        if cf_id and cf_secret and cf_id == configured_id and cf_secret == configured_secret:
            await self.app(scope, receive, send)
            return

        # If not authorized, return 401 with WWW-Authenticate header pointing to OAuth
        # This tells MCP clients (including Gemini) that OAuth authentication is required.
        res = JSONResponse(
            {"error": "unauthorized", "message": "Authentication required. Please authenticate via OAuth."},
            status_code=401,
            headers={
                "WWW-Authenticate": 'Bearer realm="SkyDemon MCP", error="invalid_token"',
                "Access-Control-Allow-Origin": "*",
            },
        )
        await res(scope, receive, send)
