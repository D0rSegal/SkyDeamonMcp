"""SkyDemon MCP server. Login cached in memory; flightplans read-only."""
from __future__ import annotations

import argparse
import os
import time
from pathlib import Path

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
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
from .logging_config import RequestLoggingMiddleware, log_tool_call, setup_logging
from .session import clear_session, ensure_session, session_status, startup_login

READ_ONLY_ANNOTATIONS = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
)


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
@log_tool_call
def skydemon_login(login: str = "", password: str = "", force: bool = False) -> dict:
    """Log in to SkyDemon. Rarely needed: server auto-logs in at startup and all online tools auto-reuse the session.

    Use when: switching user, recovering from 401 errors, or validating new credentials.
    Do NOT use before every call — call skydemon_session_status to check, other tools log in automatically.

    Args:
        login: SkyDemon username ('user' or 'user/slot'). Default: SKYDEMON_LOGIN env. Example: 'pilot@example.com'.
        password: SkyDemon password. Default: SKYDEMON_PASSWORD env. Leave empty to reuse env creds.
        force: True to force a fresh login even if a valid cached session exists. Default False (reuse cache).

    Returns: {ok, cached, licensed_to, license_type, auth_token_tail, subscription_valid_to, licenses[]}.
        On failure: {ok: False, error}.
    """
    try:
        res = ensure_session(login or None, password or None, force=force)
        st = session_status()
    except Exception as e:
        return {"ok": False, "error": str(e)}
    tok = res.authentication_token or ""
    return {
        "ok": True,
        "cached": not force and st.get("since") is not None,
        "licensed_to": res.licensed_to,
        "license_type": res.license_type,
        "auth_token_tail": f"...{tok[-4:]}" if len(tok) >= 4 else "***",
        "subscription_valid_to": st.get("subscription_valid_to"),
        "licenses": [
            {"name": l.product_name, "guid": l.product_guid, "valid_to": l.valid_to.isoformat()}
            for l in res.licenses
        ],
    }


@mcp.tool(annotations=READ_ONLY_ANNOTATIONS)
@log_tool_call
def skydemon_session_status() -> dict:
    """Check SkyDemon login state. Use before online tools to see if login is needed.

    No arguments. No login required, never fails.
    Returns: {logged_in: bool, login, licensed_to, license_type, auth_token_tail, since, subscription_valid_to, valid}.
        If not logged in: {logged_in: False}.
    """


@mcp.tool()
@log_tool_call
def skydemon_logout() -> dict:
    """Log out / drop the in-memory SkyDemon session.

    Use when: testing auth, switching user (then call skydemon_login), or clearing a bad session after 401s.
    Does NOT delete SkyDemon's own files or cloud data. Next online tool call will transparently log back in.
    Returns: {ok: True, logged_in: False}.
    """


@mcp.tool(annotations=READ_ONLY_ANNOTATIONS)
@log_tool_call
def skydemon_api_info() -> dict:
    """Diagnostics only: show SkyDemon API base URL and client identity. Not for flight planning.

    Use when: debugging connectivity or reporting issues.
    Returns: {base_url, product, device_identifier, device_type, login_endpoint, logged_in}.
    """
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


@mcp.tool(annotations=READ_ONLY_ANNOTATIONS)
@log_tool_call
def skydemon_list_flightplans() -> dict:
    """List flightplans saved on THIS PC (local disk, offline, no login needed).

    Use when: user asks 'my routes', 'saved plans', 'what flightplans do I have'.
    For cloud storage use skydemon_list_cloud_flightplans instead.
    Next step: skydemon_read_flightplan with a name from this list.
    Returns: {routes_dir, plans: [{name, path, size, modified}]}. Empty list if folder missing.
    """
    d = resolve_routes_dir()
    return {"routes_dir": str(d), "plans": list_flightplans(d)}


@mcp.tool(annotations=READ_ONLY_ANNOTATIONS)
@log_tool_call
def skydemon_read_flightplan(name: str) -> dict:
    """Read one local flightplan (offline, read-only, never modifies files).

    Use when: user picks a plan from skydemon_list_flightplans and wants route, legs, aircraft.
    Do NOT use for cloud plans — use skydemon_download_cloud_flightplan instead.

    Args:
        name: file name from the list (e.g. 'EGKK-EGCC.flightplan') or absolute path. Must end .flightplan or .gpx.

    Returns: {ok: True, file, format, aircraft: {name, registration}, routes: [{start, legs[]}], gpx_waypoints[]}.
        On failure: {ok: False, error}.
    """
    try:
        return {"ok": True, **summary_to_dict(read_flightplan(name))}
    except FileNotFoundError:
        return {"ok": False, "error": f"not found: {name}"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@mcp.tool(annotations=READ_ONLY_ANNOTATIONS)
@log_tool_call
def skydemon_search_airfields(query: str, limit: int = 20) -> dict:
    """Find airfields by ICAO or name (offline chart index, no login needed). ALWAYS start here for any airfield question.

    Use when: resolving 'Heathrow' -> EGLL, prefix 'EG' -> nearby fields, checking spelling before info/weather.
    Rank: exact ICAO first, then ICAO prefix, then name substring (biggest runways first).

    Args:
        query: ICAO code, prefix, or name fragment. Examples: 'LOWW', 'EGK', 'Innsbruck'. Empty query returns nothing.
        limit: max hits, 1-50. Default 20.

    Returns: {ok: True, count, results: [{icao, name, lat, lon, elevation_ft, chart, runways}]}.
        Call skydemon_airfield_info / skydemon_airfields_info next with an ICAO from results.
    """
    try:
        hits = af.search_airfields(query, min(max(limit, 1), 50))
    except Exception as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True, "count": len(hits),
            "results": [{k: af.airfield_to_dict(a)[k]
                         for k in ("icao", "name", "lat", "lon", "elevation_ft", "chart")}
                        | {"runways": len(a.runways)} for a in hits]}


# Offline airfield records come from static chart files, so they are cached
# per-process. Online extras (pilot notes / live feedback) are NEVER cached.
_MAX_BATCH_AIRFIELDS = 20
_OFFLINE_INFO_CACHE: dict[str, dict] = {}


def _offline_info(query: str) -> tuple[object, dict] | tuple[None, None]:
    """Resolve one airfield + cached offline dict. Returns (obj, dict) or (None, None)."""
    a = af.find_airfield(query)
    if a is None:
        return None, None
    key = (a.icao or a.name or query).strip().upper()
    cached = _OFFLINE_INFO_CACHE.get(key)
    if cached is None:
        cached = af.airfield_to_dict(a)
        _OFFLINE_INFO_CACHE[key] = cached
    return a, dict(cached)


def _attach_online(a, out: dict, token: str, intra_delay: float = 0.5) -> dict:
    """Attach pilot_notes + live_feedback, each failure captured per-key. Sleeps briefly between the two page fetches."""
    for i, (key, fn) in enumerate((("pilot_notes", af.pilot_notes_text),
                                   ("live_feedback", af.feedback_ui_text))):
        try:
            out[key] = fn(a, token)
        except Exception as e:
            out[key + "_error"] = str(e)
        if i == 0 and intra_delay > 0:
            time.sleep(min(max(intra_delay, 0), 5))
    return out


@mcp.tool(annotations=READ_ONLY_ANNOTATIONS)
@log_tool_call
def skydemon_airfield_info(icao_or_name: str, include_online: bool = True) -> dict:
    """Full info for ONE airfield: offline record + online pilot notes & live feedback.

    Use when: user asks about a single field ('runways at LOWW?', 'frequencies for EGLL?').
    For 2-20 fields in one call prefer skydemon_airfields_info (batched, throttled, offline-cached).
    Start with skydemon_search_airfields if the ICAO is unknown.

    Args:
        icao_or_name: ICAO ('LOWW') or name ('Innsbruck'). Example: 'EGLL'.
        include_online: True (default) adds pilot_notes + live_feedback (needs login, 2 page fetches).
            False = offline-only, fastest, no login, no throttling.

    Returns: {ok: True, icao, name, lat, lon, elevation_ft, runways[], frequencies[], fuel[], circuits, contacts...,
        pilot_notes?, live_feedback?, online_error?}.
        Without login: offline data + online_error set. On failure: {ok: False, error}.
    """
    a, offline = _offline_info(icao_or_name)
    if a is None:
        return {"ok": False, "error": f"airfield not found: {icao_or_name}"}
    out = {"ok": True, "query": icao_or_name, **offline}
    if not include_online:
        return out
    try:
        res = ensure_session()
        token = res.authentication_token
    except Exception as e:
        out["online_error"] = f"not logged in: {e}"
        return out
    return _attach_online(a, out, token)


@mcp.tool(annotations=READ_ONLY_ANNOTATIONS)
@log_tool_call
def skydemon_airfields_info(airfields: list[str], include_online: bool = True,
                            delay_seconds: float = 1.0) -> dict:
    """Full info for MANY airfields at once (route, alternates, comparison). Throttled + offline-cached.

    Use when: user asks about 2-20 fields ('weather/notes for LOWW, EDDM, LSZH?', 'compare runways along my route').
    Prefer this over N single skydemon_airfield_info calls — one session, polite pacing, shared offline cache.
    Do NOT use for a single field (use skydemon_airfield_info) or just to resolve names (use skydemon_search_airfields).

    Args:
        airfields: list of ICAOs or names, 1-20 items. Example: ['LOWW', 'EDDM', 'Innsbruck']. Duplicates removed, order kept.
        include_online: True (default) adds pilot_notes + live_feedback per field (needs login).
            False = offline-only for all, fastest, no login, no sleeps needed.
        delay_seconds: pause between airfields when include_online=True, to avoid server throttling.
            Default 1.0. Clamped 0.5-5.0. Plus a short 0.5s pause between the two page fetches per field.
            Ignored when include_online=False.

    Returns: {ok: True, count, include_online, results: [{query, ok: True, ...field...} | {query, ok: False, error}]}.
        One bad name fails only its own entry. Offline records come from cache; online texts are always fresh.
        Top-level {ok: False, error} only for bad input (empty list, >20 items) or login failure with include_online
        (then offline data is still returned per field with online_error set).
    """
    if not airfields or not isinstance(airfields, list):
        return {"ok": False, "error": "airfields must be a non-empty list of ICAO codes or names"}
    seen: dict[str, str] = {}
    for q in airfields:
        if isinstance(q, str) and q.strip():
            seen.setdefault(q.strip().upper(), q.strip())
    queries = list(seen.values())
    if not queries:
        return {"ok": False, "error": "airfields must be a non-empty list of ICAO codes or names"}
    if len(queries) > _MAX_BATCH_AIRFIELDS:
        return {"ok": False, "error": f"max {_MAX_BATCH_AIRFIELDS} airfields per call, got {len(queries)}"}
    delay = min(max(float(delay_seconds), 0.5), 5.0) if include_online else 0.0
    token: str | None = None
    login_error: str | None = None
    if include_online:
        try:
            token = ensure_session().authentication_token
        except Exception as e:
            login_error = str(e)
            token = None
    results: list[dict] = []
    for i, q in enumerate(queries):
        a, offline = _offline_info(q)
        if a is None:
            results.append({"query": q, "ok": False, "error": f"airfield not found: {q}"})
        else:
            out: dict = {"query": q, "ok": True, **offline}
            if not include_online:
                results.append(out)
            elif token is None:
                out["online_error"] = f"not logged in: {login_error}"
                results.append(out)
            else:
                results.append(_attach_online(a, out, token))
        if delay and i < len(queries) - 1:
            time.sleep(delay)
    return {"ok": True, "count": len(results), "include_online": include_online, "results": results}


@mcp.tool(annotations=READ_ONLY_ANNOTATIONS)
@log_tool_call
def skydemon_list_cloud_flightplans(pattern: str = "*.flightplan") -> dict:
    """List flightplans in SkyDemon CLOUD storage (online, needs login). Counterpart to local skydemon_list_flightplans.

    Use when: user asks for cloud/synced plans or is on a machine without local Routes.
    Next step: skydemon_download_cloud_flightplan with an exact name from this list.

    Args:
        pattern: server-side filter. Examples: '*.flightplan' (default), '*.gpx', 'EGKK*'. Keep broad, filter client-side.

    Returns: {ok: True, pattern, count, files: [{name, size, last_modified}]}.
        On failure (e.g. not logged in): {ok: False, error}.
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
@log_tool_call
def skydemon_download_cloud_flightplan(name: str, save: bool = False,
                                       overwrite: bool = False) -> dict:
    """Download ONE cloud plan and summarize its route (online, needs login). Only tool that can write, and only on request.

    Use when: user picks a name from skydemon_list_cloud_flightplans. Requires the EXACT cloud file name.

    Args:
        name: exact cloud file name. Example: 'EGKK-EGCC.flightplan'. Must match list output exactly.
        save: False (default, read-only summary). True = also save a copy into the local Routes dir.
        overwrite: False (default, refuse if local file exists). True = allow replacing the local copy. Only matters when save=True.

    Returns: {ok: True, name, size, last_modified, saved_to|null, file, format, aircraft{}, routes[], gpx_waypoints[], save_error?}.
        save_error set (ok still True) when save blocked. On failure: {ok: False, error}.
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


@mcp.tool(annotations=READ_ONLY_ANNOTATIONS)
@log_tool_call
def skydemon_airfield_weather(icao_or_name: str, what: str = "both") -> dict:
    """Current METAR + TAF for ONE airfield, raw bulletins (online, needs login, 5-min server cache).

    Use when: user asks 'weather at...', 'METAR/TAF for...'. Resolve names via skydemon_search_airfields first.
    You (the agent) interpret VFR/MVFR/IFR from the raw METAR text — the tool returns raw strings + timestamps only.

    Args:
        icao_or_name: ICAO ('LOWW') or name ('Innsbruck'). ICAO fastest; names resolved offline first.
        what: 'metar', 'taf', or 'both' (default). Use 'metar' for now-conditions, 'taf' for forecast, 'both' for briefing.

    Returns: {ok: True, icao, metar?: {observed, raw}, taf?: {forecast, raw}, metar_missing?, taf_missing?}.
        Missing bulletins come back null + *_missing True (still ok:True). On failure: {ok: False, error}.
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
    """Build ASGI Starlette app combining FastMCP, OAuth 2.0 endpoints, and request logging."""
    setup_logging()

    if transport == "sse":
        mcp_app = mcp.sse_app()
    else:
        mcp_app = mcp.streamable_http_app()

    # Wrap the combined app with RequestLoggingMiddleware and OAuthMiddleware
    oauth_middleware = [
        Middleware(RequestLoggingMiddleware),
        Middleware(auth.OAuthMiddleware),
    ]

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
    setup_logging()

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
