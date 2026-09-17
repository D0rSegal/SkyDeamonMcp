"""OAuth 2.0 Provider & middleware for Gemini Custom Connected Apps.

Implements:
- RFC 8414 OAuth 2.0 Authorization Server Metadata (/.well-known/oauth-authorization-server)
- OpenID Connect Discovery (/.well-known/openid-configuration)
- RFC 9728 OAuth 2.0 Protected Resource Metadata (/.well-known/oauth-protected-resource)
- RFC 7591 OAuth 2.0 Dynamic Client Registration (/oauth/register)
- RFC 7636 Proof Key for Code Exchange (PKCE)
- RFC 6749 Authorization endpoint (/oauth/authorize) and Token endpoint (/oauth/token)
- Bearer token verification ASGI middleware for MCP routes (/mcp, /sse, /messages)
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import html
import json
import logging
import os
import secrets
import time
from typing import Any, Dict
from urllib.parse import parse_qs, parse_qsl, urlencode, urlparse, urlunparse

from starlette.datastructures import Headers
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.types import ASGIApp, Receive, Scope, Send

logger = logging.getLogger("skydeamon.oauth")

# In-memory storage (with TTLs)
_AUTH_CODES: Dict[str, dict] = {}
_ACCESS_TOKENS: Dict[str, dict] = {}
_REFRESH_TOKENS: Dict[str, dict] = {}
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


def get_allowed_redirect_uris() -> list[str]:
    """Allowed redirect URIs configured via environment."""
    raw = os.environ.get("MCP_OAUTH_REDIRECT_URI", "").strip()
    if not raw:
        raw = os.environ.get("MCP_ALLOWED_REDIRECT_URIS", "").strip()
    if not raw:
        return []
    return [u.strip() for u in raw.split(",") if u.strip()]


def is_redirect_uri_allowed(uri: str) -> bool:
    """Verify whether a redirect URI is permissible."""
    if not uri:
        return False
    configured = get_allowed_redirect_uris()
    for allowed in configured:
        if uri == allowed or uri.startswith(allowed):
            return True
    # Safe default prefixes (Google user-bound redirect, localhost, testserver)
    default_prefixes = (
        "https://oauth-redirect.googleusercontent.com/r/",
        "https://gemini.google.com",
        "http://127.0.0.1",
        "http://localhost",
        "http://testserver",
    )
    return any(uri.startswith(p) for p in default_prefixes)


def get_issuer(request: Request) -> str:
    """Determine the public base URL (e.g. https://flights.segal.to)."""
    forwarded_proto = request.headers.get("x-forwarded-proto", "https")
    host = request.headers.get("x-forwarded-host") or request.headers.get("host") or "flights.segal.to"
    return f"{forwarded_proto}://{host}"


def get_issuer_from_scope(scope: Scope) -> str:
    """Determine public base URL from ASGI scope."""
    headers = Headers(scope=scope)
    forwarded_proto = headers.get("x-forwarded-proto", "https")
    host = headers.get("x-forwarded-host") or headers.get("host") or "flights.segal.to"
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
        "grant_types_supported": ["authorization_code", "client_credentials", "refresh_token"],
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
        "resource": f"{base}/mcp",
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
    configured_id, configured_secret = get_oauth_credentials()
    client_id = configured_id or f"client_{secrets.token_hex(8)}"
    client_secret = configured_secret or secrets.token_urlsafe(32)
    _DYNAMIC_CLIENTS[client_id] = {
        "client_secret": client_secret,
        "client_name": body.get("client_name", "Dynamic Client"),
        "redirect_uris": body.get("redirect_uris", []),
        "created_at": time.time(),
    }
    logger.info(f"[oauth] registered client_id={client_id}")
    return JSONResponse({
        "client_id": client_id,
        "client_secret": client_secret,
        "client_name": body.get("client_name", "Dynamic Client"),
        "redirect_uris": body.get("redirect_uris", []),
        "grant_types": ["authorization_code", "client_credentials", "refresh_token"],
        "response_types": ["code"],
    }, status_code=201)


async def oauth_authorize_endpoint(request: Request) -> Response:
    """Interactive / programmatic authorization endpoint."""
    logger.info(f"[oauth] authorize hit: {request.method} {request.url.path}")

    # If user submitted the login form
    if request.method == "POST":
        form = await request.form()
        action = form.get("action", "")
        client_id = str(form.get("client_id") or request.query_params.get("client_id", ""))
        redirect_uri = str(form.get("redirect_uri") or request.query_params.get("redirect_uri", ""))
        state = str(form.get("state") or request.query_params.get("state", ""))
        code_challenge = str(form.get("code_challenge") or request.query_params.get("code_challenge", ""))
        code_challenge_method = str(form.get("code_challenge_method") or request.query_params.get("code_challenge_method", "plain"))

        if not is_redirect_uri_allowed(redirect_uri):
            logger.warning(f"[oauth] reject redirect_uri: {redirect_uri}")
            return HTMLResponse(f"<h3>Error</h3><p>Redirect URI not allowed: {html.escape(redirect_uri)}</p>", status_code=400)

        if action == "approve":
            code = secrets.token_urlsafe(32)
            _AUTH_CODES[code] = {
                "client_id": client_id,
                "redirect_uri": redirect_uri,
                "code_challenge": code_challenge,
                "code_challenge_method": code_challenge_method,
                "expires_at": time.time() + CODE_TTL_SEC,
            }
            logger.info(f"[oauth] approved client_id={client_id}; issuing code={code[:8]}... redirecting to {redirect_uri}")

            parsed = urlparse(redirect_uri)
            qs = dict(parse_qsl(parsed.query, keep_blank_values=True))
            qs["code"] = code
            if state:
                qs["state"] = state
            new_query = urlencode(qs)
            target = urlunparse((parsed.scheme, parsed.netloc, parsed.path, parsed.params, new_query, parsed.fragment))
            return RedirectResponse(target, status_code=302)
        else:
            logger.info(f"[oauth] denied by user; redirecting to {redirect_uri}")
            parsed = urlparse(redirect_uri)
            qs = dict(parse_qsl(parsed.query, keep_blank_values=True))
            qs["error"] = "access_denied"
            if state:
                qs["state"] = state
            target = urlunparse((parsed.scheme, parsed.netloc, parsed.path, parsed.params, urlencode(qs), parsed.fragment))
            return RedirectResponse(target, status_code=302)

    # GET request
    params = request.query_params
    client_id = params.get("client_id", "")
    redirect_uri = params.get("redirect_uri", "")
    state = params.get("state", "")
    code_challenge = params.get("code_challenge", "")
    code_challenge_method = params.get("code_challenge_method", "")
    scope = params.get("scope", "")

    if redirect_uri and not is_redirect_uri_allowed(redirect_uri):
        logger.warning(f"[oauth] reject redirect_uri: {redirect_uri}")
        return HTMLResponse(f"<h3>Error</h3><p>Redirect URI not allowed: {html.escape(redirect_uri)}</p>", status_code=400)

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
        .client-info {{
            font-size: 13px;
            color: #64748b;
            margin: 12px 0 20px;
            word-break: break-all;
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
        <div class="client-info">Client: <strong>{html.escape(client_id or "gemini")}</strong></div>
        <form method="POST">
            <input type="hidden" name="client_id" value="{html.escape(client_id)}">
            <input type="hidden" name="redirect_uri" value="{html.escape(redirect_uri)}">
            <input type="hidden" name="state" value="{html.escape(state)}">
            <input type="hidden" name="code_challenge" value="{html.escape(code_challenge)}">
            <input type="hidden" name="code_challenge_method" value="{html.escape(code_challenge_method)}">
            <input type="hidden" name="scope" value="{html.escape(scope)}">
            <div class="actions">
                <button type="submit" name="action" value="deny" class="btn-secondary">Deny</button>
                <button type="submit" name="action" value="approve" class="btn-primary">Approve &amp; Connect</button>
            </div>
        </form>
    </div>
</body>
</html>"""
    return HTMLResponse(html_content)


async def oauth_token_endpoint(request: Request) -> JSONResponse:
    """Token exchange endpoint supporting authorization_code, client_credentials, and refresh_token."""
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
    redirect_uri = body.get("redirect_uri", "")
    code_verifier = body.get("code_verifier", "")

    # Basic auth check if client credentials passed via Authorization header
    auth_header = request.headers.get("authorization", "")
    if auth_header.startswith("Basic "):
        try:
            decoded = base64.b64decode(auth_header[6:]).decode("utf-8")
            if ":" in decoded:
                client_id, client_secret = decoded.split(":", 1)
        except Exception:
            pass

    configured_id, configured_secret = get_oauth_credentials()
    logger.info(
        f"[oauth] token request: grant_type={grant_type} client_id={client_id} "
        f"has_secret={bool(client_secret)} has_code={bool(code)} has_verifier={bool(code_verifier)}"
    )

    # Validate secret if client passed one
    if client_id == configured_id and configured_secret and client_secret and client_secret != configured_secret:
        logger.warning("[oauth] token request: client_secret mismatch")
        return JSONResponse({"error": "invalid_client", "error_description": "client_secret mismatch"}, status_code=401)

    now = time.time()

    if grant_type == "authorization_code":
        record = _AUTH_CODES.pop(code, None)
        if not record or record["expires_at"] < now:
            logger.warning("[oauth] token error: invalid or expired authorization code")
            return JSONResponse({"error": "invalid_grant", "error_description": "code expired or invalid"}, status_code=400)

        # Validate redirect_uri if supplied (RFC 6749 Section 4.1.3)
        expected_redirect = record.get("redirect_uri", "")
        if redirect_uri and expected_redirect and redirect_uri != expected_redirect:
            logger.warning(f"[oauth] token error: redirect_uri mismatch '{redirect_uri}' != '{expected_redirect}'")
            return JSONResponse({"error": "invalid_grant", "error_description": "redirect_uri mismatch"}, status_code=400)

        # Validate PKCE code_verifier (RFC 7636)
        ch = record.get("code_challenge")
        chm = record.get("code_challenge_method", "plain")
        if ch:
            if not code_verifier:
                logger.warning("[oauth] token error: code_verifier required for PKCE")
                return JSONResponse({"error": "invalid_grant", "error_description": "code_verifier required"}, status_code=400)
            if chm == "S256":
                digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
                computed = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
                if computed != ch:
                    logger.warning(f"[oauth] token error: PKCE S256 verification failed (computed {computed} != challenge {ch})")
                    return JSONResponse({"error": "invalid_grant", "error_description": "code_verifier mismatch"}, status_code=400)
            elif ch != code_verifier:
                logger.warning("[oauth] token error: PKCE plain verification failed")
                return JSONResponse({"error": "invalid_grant", "error_description": "code_verifier mismatch"}, status_code=400)

        # Generate Bearer access token
        token = f"mcp_{secrets.token_urlsafe(32)}"
        refresh_token = f"ref_{secrets.token_urlsafe(32)}"
        _ACCESS_TOKENS[token] = {
            "client_id": client_id or configured_id,
            "expires_at": now + TOKEN_TTL_SEC,
        }
        _REFRESH_TOKENS[refresh_token] = {
            "client_id": client_id or configured_id,
            "expires_at": now + (TOKEN_TTL_SEC * 2),
        }
        logger.info(f"[oauth] token success: issued access_token & refresh_token for client_id={client_id}")

        return JSONResponse({
            "access_token": token,
            "token_type": "Bearer",
            "expires_in": TOKEN_TTL_SEC,
            "refresh_token": refresh_token,
            "scope": "mcp",
        }, headers={"Cache-Control": "no-store", "Access-Control-Allow-Origin": "*"})

    elif grant_type == "client_credentials":
        token = f"mcp_{secrets.token_urlsafe(32)}"
        refresh_token = f"ref_{secrets.token_urlsafe(32)}"
        _ACCESS_TOKENS[token] = {
            "client_id": client_id or configured_id,
            "expires_at": now + TOKEN_TTL_SEC,
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
        new_refresh = f"ref_{secrets.token_urlsafe(32)}"
        _ACCESS_TOKENS[token] = {
            "client_id": client_id or configured_id,
            "expires_at": now + TOKEN_TTL_SEC,
        }
        _REFRESH_TOKENS[new_refresh] = {
            "client_id": client_id or configured_id,
            "expires_at": now + (TOKEN_TTL_SEC * 2),
        }
        return JSONResponse({
            "access_token": token,
            "token_type": "Bearer",
            "expires_in": TOKEN_TTL_SEC,
            "refresh_token": new_refresh,
            "scope": "mcp",
        }, headers={"Cache-Control": "no-store", "Access-Control-Allow-Origin": "*"})

    logger.warning(f"[oauth] unsupported grant_type: {grant_type}")
    return JSONResponse({"error": "unsupported_grant_type"}, status_code=400)


def is_token_valid(token: str) -> bool:
    """Verify if Bearer token is valid."""
    # Allow master secret directly as Bearer token too
    _, master_secret = get_oauth_credentials()
    if master_secret and token == master_secret:
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

        # If not authorized, return 401 with RFC 9728 WWW-Authenticate header
        base = get_issuer_from_scope(scope)
        res = JSONResponse(
            {"error": "unauthorized", "message": "Authentication required. Please authenticate via OAuth."},
            status_code=401,
            headers={
                "WWW-Authenticate": f'Bearer resource_metadata="{base}/.well-known/oauth-protected-resource"',
                "Access-Control-Allow-Origin": "*",
            },
        )
        await res(scope, receive, send)
