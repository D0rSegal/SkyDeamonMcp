"""SkyDemon Login API — clean-room reimplementation of LoginHelper.Login.

Replicates (tmp/decompiled/SkyDemon.decompiled.cs ~22670-22765):
  POST https://data.skydemon.aero/Login/LoginDevice
  JSON: {"Login","Slot","Password","DeviceIdentifier","DeviceType"}
  Response: DeviceLogin binary blob (magic 64236213, SHA1-checked licenses).

No SkyDemon code is copied — only the wire format is reproduced for interop.
"""
from __future__ import annotations

import base64
import hashlib
import io
import os
import platform
import struct
import subprocess
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Tuple

import httpx

from .config import (
    DEVICE_LOGIN_MAGIC,
    LICENSES_UPDATED_GUID,
    PLANNING_PRODUCT_GUID,
    PLATE_PRODUCT_GUID,
    PRODUCT_NAME,
    PRODUCT_VERSION,
    get_url_for_api,
)

LOGIN_API = "Login/LoginDevice"
_FILETIME_EPOCH = datetime(1601, 1, 1, tzinfo=timezone.utc)


# ---------------------------------------------------------------- devices ---

def get_machine_guid() -> str | None:
    """HKLM\\SOFTWARE\\Microsoft\\Cryptography\\MachineGuid (StorageServices.GetMachineGuid)."""
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography"
        ) as key:
            val, _ = winreg.QueryValueEx(key, "MachineGuid")
        if val and len(str(val)) >= 20:
            return str(val)
    except Exception:
        pass
    return None


def get_mac_address() -> str | None:
    """Fastest physical address >= 12 hex chars (StorageServices.GetMacAddress)."""
    try:
        node = uuid.getnode()
        mac = f"{node:012X}"
        if len(mac) >= 12 and node != 0:
            return mac
    except Exception:
        pass
    return None


def get_fallback_id() -> str:
    s = f"FB|{platform.node().upper()}|{os.environ.get('USERNAME', '?').upper()}|{os.cpu_count() or 1}"
    return base64.b64encode(hashlib.sha1(s.encode("utf-8")).digest()).decode("ascii")


def get_device_identifier() -> str:
    guid = get_machine_guid() or get_mac_address() or get_fallback_id()
    return f"P|{guid}"


def get_device_type() -> str:
    """'Manufacturer|Model|MachineName' like StorageServices.DeviceType."""
    machine = platform.node()
    try:
        out = subprocess.run(
            ["wmic", "computersystem", "get", "manufacturer,model", "/format:list"],
            capture_output=True, text=True, timeout=10,
        )
        manu = model = ""
        for line in (out.stdout or "").splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                k, v = k.strip().lower(), v.strip()
                if k == "manufacturer":
                    manu = v
                elif k == "model":
                    model = v
        if manu or model:
            return f"{manu}|{model}|{machine}".strip()
    except Exception:
        pass
    return f"PC, {machine}"


def split_login_slot(login: str) -> Tuple[str, int]:
    """'user' -> ('user', -1); 'user/3' -> ('user', 3). Mirrors Login()."""
    if "/" in login:
        head, tail = login.split("/", 1)
        try:
            return head, int(tail.strip())
        except ValueError:
            return head, -1
    return login, -1


# ------------------------------------------------------- binary encoding ----

def _read_7bit_int(buf: io.BytesIO) -> int:
    result = 0
    shift = 0
    while True:
        b = buf.read(1)
        if not b:
            raise ValueError("truncated 7-bit int")
        byte = b[0]
        result |= (byte & 0x7F) << shift
        shift += 7
        if not (byte & 0x80):
            return result


def _read_cs_string(buf: io.BytesIO) -> str:
    n = _read_7bit_int(buf)
    data = buf.read(n)
    if len(data) != n:
        raise ValueError("truncated C# string")
    return data.decode("utf-8")


def _write_cs_string(buf: io.BytesIO, s: str) -> None:
    data = s.encode("utf-8")
    n = len(data)
    while n >= 0x80:
        buf.write(bytes([(n & 0x7F) | 0x80]))
        n >>= 7
    buf.write(bytes([n]))
    buf.write(data)


def _filetime_to_dt(ft: int) -> datetime:
    return _FILETIME_EPOCH + timedelta(microseconds=ft // 10)


@dataclass
class ProductLicense:
    product_name: str
    product_guid: str
    valid_to: datetime
    is_plates: bool = False


@dataclass
class DeviceLoginResult:
    authentication_token: str
    licensed_to: str
    license_type: str
    licenses_last_updated: datetime | None
    licenses: list = field(default_factory=list)


def _parse_license_type_and_to(product_name: str) -> Tuple[str, str]:
    # Mirrors DeviceLogin.ParseLicenseTypeAndTo
    if "|" in product_name:
        prefix, rest = product_name.split("|", 1)
        mapping = {"CP": "Corporate", "FC": "Device", "EVAL": "Evaluation"}
        return mapping.get(prefix, "Personal"), rest
    return "Personal", product_name


def parse_device_login(blob: bytes) -> DeviceLoginResult:
    """Parse DeviceLogin.Deserialize output. Raises ValueError on corruption."""
    buf = io.BytesIO(blob)
    (magic,) = struct.unpack("<i", buf.read(4))
    if magic != DEVICE_LOGIN_MAGIC:
        raise ValueError(f"bad magic {magic}, expected {DEVICE_LOGIN_MAGIC}")

    auth_token = ""
    licensed_to = ""
    license_type = "Personal"
    last_updated = None
    licenses: list[ProductLicense] = []

    while buf.tell() < len(blob):
        (n,) = struct.unpack("<i", buf.read(4))
        data = buf.read(n)
        (hn,) = struct.unpack("<i", buf.read(4))
        want_hash = buf.read(hn)
        if len(data) != n or len(want_hash) != hn:
            raise ValueError("truncated license envelope")
        if hashlib.sha1(data).digest() != want_hash:
            continue  # hash mismatch -> skip entry, like DeserializeLicense == null
        inner = io.BytesIO(data)
        token_guid = str(uuid.UUID(bytes_le=inner.read(16)))
        product_guid = str(uuid.UUID(bytes_le=inner.read(16)))
        product_name = _read_cs_string(inner)
        (ft,) = struct.unpack("<q", inner.read(8))
        valid_to = _filetime_to_dt(ft)
        if product_guid.lower() == LICENSES_UPDATED_GUID:
            last_updated = valid_to
            continue
        is_plates = token_guid.lower() == PLATE_PRODUCT_GUID
        licenses.append(ProductLicense(product_name, product_guid, valid_to, is_plates))
        if product_guid.lower() == PLANNING_PRODUCT_GUID:
            auth_token = token_guid
            license_type, licensed_to = _parse_license_type_and_to(product_name)

    if not auth_token or not licensed_to:
        raise ValueError("login blob missing planning license")
    return DeviceLoginResult(auth_token, licensed_to, license_type, last_updated, licenses)


# ------------------------------------------------------------------ login ---

def build_login_payload(login: str, password: str,
                        device_id: str | None = None,
                        device_type: str | None = None) -> dict:
    user, slot = split_login_slot(login)
    return {
        "Login": user,
        "Slot": slot,
        "Password": password,
        "DeviceIdentifier": device_id or get_device_identifier(),
        "DeviceType": device_type or get_device_type(),
    }


def login_device(login: str, password: str, *,
                 device_id: str | None = None,
                 device_type: str | None = None,
                 timeout: float = 20.0) -> DeviceLoginResult:
    """POST Login/LoginDevice and parse the DeviceLogin blob."""
    payload = build_login_payload(login, password, device_id, device_type)
    did = payload["DeviceIdentifier"]
    headers = {
        "Content-Type": "application/json",
        "Accept": "*/*",
        "User-Agent": f"{PRODUCT_NAME} (Version {PRODUCT_VERSION}; Device {did})",
    }
    url = get_url_for_api(LOGIN_API)
    try:
        resp = httpx.post(url, json=payload, headers=headers, timeout=timeout)
    except httpx.TimeoutException as e:
        raise RuntimeError("SkyDemon request timed out") from e
    except httpx.RequestError as e:
        raise RuntimeError(f"SkyDemon request failed: {e}") from e
    if resp.status_code == 401:
        reason = resp.headers.get("X-SkyDemon-Reason", "")
        body = resp.text.strip()[:300] if "text/plain" in resp.headers.get("content-type", "") else ""
        raise RuntimeError(f"login rejected (401 reason={reason}): {body}")
    if resp.status_code < 200 or resp.status_code >= 300:
        body = resp.text.strip()[:300]
        raise RuntimeError(f"login failed HTTP {resp.status_code}: {body}")
    try:
        return parse_device_login(resp.content)
    except ValueError as e:
        raise RuntimeError(f"login response corrupt: {e}") from e
