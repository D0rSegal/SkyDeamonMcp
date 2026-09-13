"""SkyDemon MCP server (stdio). Login cached in memory; flightplans read-only."""
from __future__ import annotations

import os

from mcp.server.fastmcp import FastMCP

from .api import get_device_identifier, get_device_type
from . import airfields as af
from . import cloud as cl
from .config import BASE_URL, PRODUCT_NAME, PRODUCT_VERSION
from .flightplans import (
    list_flightplans,
    read_flightplan,
    resolve_routes_dir,
    summary_to_dict,
)
from .session import clear_session, ensure_session, session_status, startup_login

mcp = FastMCP("skydemon")


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


def main() -> None:
    # Login once at startup so tools reuse the in-memory session.
    if os.environ.get("SKYDEMON_LOGIN") and os.environ.get("SKYDEMON_PASSWORD"):
        startup_login()
    mcp.run()


if __name__ == "__main__":
    main()
