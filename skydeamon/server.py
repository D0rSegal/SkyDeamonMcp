"""SkyDemon MCP server. Login cached in memory; flightplans read-only."""
from __future__ import annotations

import argparse
import os
from pathlib import Path

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.routing import Mount, Route
import uvicorn

from . import auth
from .api import get_device_identifier, get_device_type
from . import airfields as af
from . import cloud as cl
from . import weather as wx
from .config import BASE_URL, PRODUCT_NAME, PRODUCT_VERSION
from .flightplans import (
    list_flightplans,
    read_flightplan,
    resolve_routes_dir,
    summary_to_dict,
)
from .session import clear_session, ensure_session, session_status, startup_login

def _public_hosts() -> list[str]:
    """Extra Host values allowed through DNS-rebinding protection.

    Needed when the server sits behind a tunnel/proxy that forwards the
    public hostname (e.g. Cloudflare Tunnel -> flights.segal.to).
    Override with SKYDEMON_ALLOWED_HOSTS="a.example,b.example".
    """
    extra = os.environ.get("SKYDEMON_ALLOWED_HOSTS", "")
    hosts = [h.strip() for h in extra.split(",") if h.strip()]
    if not hosts:
        hosts = ["flights.segal.to", "origin.segal.to"]
    return hosts


mcp = FastMCP(
    "skydemon",
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        # NOTE: ":*" entries only match Host WITH a port; bare names are
        # needed too because proxies forward "flights.segal.to" portless.
        allowed_hosts=["127.0.0.1:*", "localhost:*", "[::1]:*", "testserver", "testserver:*"]
        + [h for h in _public_hosts()]
        + [f"{h}:*" for h in _public_hosts()],
        allowed_origins=["http://127.0.0.1:*", "http://localhost:*", "http://[::1]:*", "http://testserver", "http://testserver:*"]
        + [f"https://{h}:*" for h in _public_hosts()],
    ),
)


@mcp.tool()
def skydemon_login(login: str = "", password: str = "", force: bool = False) -> dict:
    """Log in once; session is held in memory and reused (no repeated logins).

    Args:
        login: username ('user' or 'user/slot'); defaults to SKYDEMON_LOGIN.
        password: password; defaults to SKYDEMON_PASSWORD env.
        force: true to force a fresh login even if a valid session exists.
    """
    try:
        res = ensure_session(login or None, password or None, force=force)
        st = session_status()
    except Exception as e:
        return {"ok": False, "error": str(e)}
    return {
        "ok": True,
        "cached": not force and st.get("since") is not None,
        "licensed_to": res.licensed_to,
        "license_type": res.license_type,
        "auth_token": res.authentication_token,
        "subscription_valid_to": st.get("subscription_valid_to"),
        "licenses": [
            {"name": l.product_name, "guid": l.product_guid, "valid_to": l.valid_to.isoformat()}
            for l in res.licenses
        ],
    }


@mcp.tool()
def skydemon_session_status() -> dict:
    """Show cached login state (token tail only, never the full secret)."""
    return session_status()


@mcp.tool()
def skydemon_logout() -> dict:
    """Drop the in-memory session (does not touch SkyDemon's own files)."""
    clear_session()
    return {"ok": True, "logged_in": False}


@mcp.tool()
def skydemon_api_info() -> dict:
    """Show SkyDemon API base + client identity used by this wrapper."""
    st = session_status()
    return {
        "base_url": BASE_URL,
        "product": f"{PRODUCT_NAME} {PRODUCT_VERSION}",
        "device_identifier": get_device_identifier(),
        "device_type": get_device_type(),
        "login_endpoint": f"{BASE_URL}/Login/LoginDevice",
        "logged_in": st.get("logged_in", False),
        "notes": "Wire format reverse-engineered from SkyDemon.exe (LoginHelper/DeviceLogin). No vendor code shipped.",
    }


@mcp.tool()
def skydemon_list_flightplans() -> dict:
    """List saved flightplans on disk (read-only). Default: ~/Documents/SkyDemon/Routes."""
    d = resolve_routes_dir()
    return {"routes_dir": str(d), "plans": list_flightplans(d)}


@mcp.tool()
def skydemon_read_flightplan(name: str) -> dict:
    """Read one flightplan from disk (read-only, no edits).

    Args:
        name: file name (e.g. 'EGKK-EGCC.flightplan') or absolute path.
    """
    try:
        return {"ok": True, **summary_to_dict(read_flightplan(name))}
    except FileNotFoundError:
        return {"ok": False, "error": f"not found: {name}"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@mcp.tool()
def skydemon_search_airfields(query: str, limit: int = 20) -> dict:
    """General airfield search over installed charts (offline, read-only).

    Matches ICAO exactly / by prefix, then name substring. Biggest
    airfields rank first.
    """
    try:
        hits = af.search_airfields(query, min(max(limit, 1), 50))
    except Exception as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True, "count": len(hits),
            "results": [{k: af.airfield_to_dict(a)[k]
                         for k in ("icao", "name", "lat", "lon", "elevation_ft", "chart")}
                        | {"runways": len(a.runways)} for a in hits]}


@mcp.tool()
def skydemon_airfield_info(icao_or_name: str, include_online: bool = True) -> dict:
    """Full airfield info: offline record (runways, frequencies, fuel,
    circuits, contacts) + online pilot notes & live feedback text.

    Online extras need a login session; without one only offline data
    is returned (online_error set).
    """
    a = af.find_airfield(icao_or_name)
    if a is None:
        return {"ok": False, "error": f"airfield not found: {icao_or_name}"}
    out = {"ok": True, **af.airfield_to_dict(a)}
    if not include_online:
        return out
    try:
        res = ensure_session()
        token = res.authentication_token
    except Exception as e:
        out["online_error"] = f"not logged in: {e}"
        return out
    for key, fn in (("pilot_notes", af.pilot_notes_text),
                    ("live_feedback", af.feedback_ui_text)):
        try:
            out[key] = fn(a, token)
        except Exception as e:
            out[key + "_error"] = str(e)
    return out


@mcp.tool()
def skydemon_list_cloud_flightplans(pattern: str = "*.flightplan") -> dict:
    """List flightplans in SkyDemon cloud storage (User root, read-only).

    Args:
        pattern: server-side filter, e.g. '*.flightplan' or '*.gpx'.
    """
    try:
        res = ensure_session()
        files = cl.list_cloud_files(res.authentication_token, pattern)
    except Exception as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True, "pattern": pattern, "count": len(files),
            "files": [{"name": f.name, "size": f.size,
                       "last_modified": f.last_modified.isoformat()} for f in files]}


@mcp.tool()
def skydemon_download_cloud_flightplan(name: str, save: bool = False,
                                       overwrite: bool = False) -> dict:
    """Download one cloud flightplan and summarize it (no edits by default).

    Args:
        name: exact cloud file name from skydemon_list_cloud_flightplans.
        save: also save a copy into the local Routes dir.
        overwrite: allow replacing an existing local file when saving.
    """
    from .flightplans import resolve_routes_dir, summarize_flightplan_bytes, summary_to_dict
    try:
        res = ensure_session()
        downloads = cl.download_cloud_files(res.authentication_token, [name])
    except Exception as e:
        return {"ok": False, "error": str(e)}
    if not downloads:
        return {"ok": False, "error": f"not found in cloud: {name}"}
    dl = downloads[0]
    try:
        summary = summary_to_dict(summarize_flightplan_bytes(dl.contents, dl.name))
    except Exception as e:
        summary = {"parse_error": str(e)}
    out = {"ok": True, "name": dl.name, "size": len(dl.contents),
           "last_modified": dl.last_modified.isoformat(), "saved_to": None,
           **summary}
    if save:
        dest = resolve_routes_dir() / dl.name
        if dest.exists() and not overwrite:
            out["save_error"] = f"exists (pass overwrite=true): {dest}"
            return out
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(dl.contents)
            out["saved_to"] = str(dest)
        except OSError as e:
            out["save_error"] = str(e)
    return out


def _load_env(path: Path | None = None) -> None:
    """Load project-root .env silently (never prints values).

    Real environment always wins (no override). Works with python-dotenv
    when installed; falls back to a tiny built-in parser otherwise.
    """
    dotenv_path = path or Path(__file__).resolve().parent.parent / ".env"
    try:
        text = dotenv_path.read_text(encoding="utf-8")
    except OSError:
        return
    try:
        from dotenv import load_dotenv
    except ImportError:
        load_dotenv = None
    if load_dotenv is not None:
        load_dotenv(dotenv_path, override=False)
        return
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        key, val = key.strip(), val.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = val


@mcp.tool()
def skydemon_airfield_weather(icao_or_name: str, what: str = "both") -> dict:
    """Current METAR + TAF for an airfield (raw bulletins, needs login).

    Args:
        icao_or_name: ICAO code ('LOWW') or airfield name (resolved offline).
        what: 'metar', 'taf', or 'both'. VFR/IFR assessment is left to the
            caller from the raw METAR text.
    """
    if what not in ("metar", "taf", "both"):
        return {"ok": False, "error": "what must be metar, taf or both"}
    key = icao_or_name.strip().upper()
    if not wx.is_icao(key):
        found = af.find_airfield(icao_or_name)
        if found is None or not found.icao:
            return {"ok": False, "error": f"airfield not found: {icao_or_name}"}
        key = found.icao.upper()
    try:
        res = ensure_session()
        (w,) = wx.get_weather(res.authentication_token, [key]).values()
    except Exception as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True, **wx.weather_to_dict(w, what)}


def build_http_app(transport: str = "streamable-http") -> Starlette:
    """Build ASGI Starlette app combining FastMCP and OAuth 2.0 endpoints."""
    if transport == "sse":
        mcp_app = mcp.sse_app()
    else:
        mcp_app = mcp.streamable_http_app()

    # Wrap the combined app with OAuthMiddleware for Bearer token validation
    oauth_middleware = [Middleware(auth.OAuthMiddleware)]

    oauth_routes = [
        Route("/.well-known/oauth-authorization-server", endpoint=auth.oauth_metadata_response, methods=["GET"]),
        Route("/.well-known/openid-configuration", endpoint=auth.oauth_metadata_response, methods=["GET"]),
        Route("/.well-known/oauth-protected-resource", endpoint=auth.oauth_protected_resource_metadata, methods=["GET"]),
        Route("/.well-known/oauth-protected-resource/mcp", endpoint=auth.oauth_protected_resource_metadata, methods=["GET"]),
        Route("/oauth/authorize", endpoint=auth.oauth_authorize_endpoint, methods=["GET", "POST"]),
        Route("/oauth/token", endpoint=auth.oauth_token_endpoint, methods=["POST"]),
        Route("/oauth/register", endpoint=auth.oauth_register_endpoint, methods=["POST"]),
        Mount("/", app=mcp_app),
    ]
    # Forward lifespan so that session_manager task group initializes properly
    lifespan = getattr(mcp_app.router, "lifespan_context", None)
    return Starlette(routes=oauth_routes, middleware=oauth_middleware, lifespan=lifespan)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="skydeamon-mcp",
                                     description="SkyDemon MCP server")
    parser.add_argument("--transport", default="stdio",
                        choices=["stdio", "streamable-http", "sse"],
                        help="MCP transport (default: stdio; 'sse' is deprecated upstream)")
    parser.add_argument("--host", default="127.0.0.1",
                        help="HTTP bind host (streamable-http/sse only)")
    parser.add_argument("--port", type=int, default=8000,
                        help="HTTP bind port (streamable-http/sse only)")
    args = parser.parse_args(argv)
    _load_env()
    # Login once at startup so tools reuse the in-memory session.
    if os.environ.get("SKYDEMON_LOGIN") and os.environ.get("SKYDEMON_PASSWORD"):
        startup_login()
    if args.transport == "stdio":
        mcp.run()
    else:
        # mcp 1.x takes bind host/port from settings, transport from run()
        mcp.settings.host = args.host
        mcp.settings.port = args.port
        app = build_http_app(transport=args.transport)
        uvicorn.run(app, host=args.host, port=args.port, log_level=mcp.settings.log_level.lower())


if __name__ == "__main__":
    main()

