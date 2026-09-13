"""In-memory SkyDemon session — login once on startup, reuse until expiry.

Mirrors the app behaviour (LicensingService caches DeviceLogin) without
writing anything to disk. Passwords are never stored; only the auth token
+ license summary live in this process.
"""
from __future__ import annotations

import os
import sys
import threading
from datetime import datetime, timezone, timedelta
from typing import Optional

from .api import DeviceLoginResult, login_device
from .config import PLANNING_PRODUCT_GUID

_lock = threading.Lock()
_session: dict = {"result": None, "login": None, "at": None}


def _stderr(msg: str) -> None:
    print(f"[skydemon] {msg}", file=sys.stderr)


def _subscription_expiry(res: DeviceLoginResult) -> Optional[datetime]:
    for lic in res.licenses:
        if lic.product_guid.lower() == PLANNING_PRODUCT_GUID:
            return lic.valid_to
    return None


def is_session_valid(margin: timedelta = timedelta(hours=1)) -> bool:
    with _lock:
        res = _session["result"]
    if res is None:
        return False
    exp = _subscription_expiry(res)
    if exp is None:
        return True  # no expiry info -> assume valid
    now = datetime.now(timezone.utc)
    if exp.tzinfo is None:
        exp = exp.replace(tzinfo=timezone.utc)
    return now < (exp - margin)


def ensure_session(login: Optional[str] = None,
                   password: Optional[str] = None,
                   force: bool = False) -> DeviceLoginResult:
    """Return cached DeviceLoginResult, logging in once if needed.

    Raises RuntimeError if credentials are missing or login fails.
    """
    login = (login or os.environ.get("SKYDEMON_LOGIN", "")).strip()
    password = password or os.environ.get("SKYDEMON_PASSWORD", "")
    if not login or not password:
        raise RuntimeError("SKYDEMON_LOGIN / SKYDEMON_PASSWORD not set (env or args)")

    with _lock:
        cached = _session["result"]
        cached_login = _session["login"]
    if cached is not None and not force and cached_login == login and is_session_valid():
        return cached

    # single fresh login (outside lock would allow stampedes; keep it simple:
    # hold lock — logins are rare and fast enough)
    with _lock:
        # re-check after acquiring
        if (_session["result"] is not None and not force
                and _session["login"] == login and is_session_valid()):
            return _session["result"]
        res = login_device(login, password)
        _session["result"] = res
        _session["login"] = login
        _session["at"] = datetime.now(timezone.utc)
        os.environ["SKYDEMON_AUTH_TOKEN"] = res.authentication_token
        return res


def session_status() -> dict:
    with _lock:
        res = _session["result"]
        login = _session["login"]
        at = _session["at"]
    if res is None:
        return {"logged_in": False}
    exp = _subscription_expiry(res)
    tok = res.authentication_token or ""
    return {
        "logged_in": True,
        "login": login,
        "licensed_to": res.licensed_to,
        "license_type": res.license_type,
        "auth_token_tail": tok[-4:] if len(tok) >= 4 else "***",
        "since": at.isoformat() if at else None,
        "subscription_valid_to": exp.isoformat() if exp else None,
        "valid": is_session_valid(),
    }


def clear_session() -> None:
    with _lock:
        _session["result"] = None
        _session["login"] = None
        _session["at"] = None
    os.environ.pop("SKYDEMON_AUTH_TOKEN", None)


def startup_login() -> bool:
    """Called once in main() — best-effort login so later tools reuse it."""
    if not os.environ.get("SKYDEMON_LOGIN") or not os.environ.get("SKYDEMON_PASSWORD"):
        _stderr("no credentials in env, skipping startup login")
        return False
    try:
        res = ensure_session()
        _stderr(f"startup login ok for {res.licensed_to} ({res.license_type})")
        return True
    except Exception as e:
        _stderr(f"startup login failed: {e}")
        return False
