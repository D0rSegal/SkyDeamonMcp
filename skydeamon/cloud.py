"""SkyDemon cloud storage — list + download (no upload/delete).

Clean-room reimplementation of (tmp/decompiled/SkyDemon.decompiled.cs):
- CloudStorageJob base (~85535): ServiceUrlNew "Cloud/User/", Bearer via ServerQuery
- CloudStorageListFilesJob (~85261): POST Cloud/User/ListFiles?pattern=..&includedeleted=..
  JSON {"Pattern","IncludeDeleted","ExtendedProperties"} -> XML <CloudStorageResults>
- CloudStorageGetFilesJob (~85195): POST Cloud/User/GetFiles?na
  JSON {"Filenames":[...]} -> binary: C# string, int32 len, FILETIME, bytes
"""
from __future__ import annotations

import io
import struct
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import httpx

LIST_TIMEOUT = 60.0


@dataclass
class CloudFile:
    name: str
    size: int
    last_modified: datetime


def _filetime_to_dt(ft: int) -> datetime:
    return datetime(1601, 1, 1, tzinfo=timezone.utc) + timedelta(
        microseconds=ft // 10)


def _client(token: str, timeout: float = 20.0) -> httpx.Client:
    from .config import BASE_URL, PRODUCT_NAME, PRODUCT_VERSION
    from .api import get_device_identifier
    return httpx.Client(
        base_url=BASE_URL,
        headers={
            "User-Agent": f"{PRODUCT_NAME} (Version {PRODUCT_VERSION}; "
                          f"Device {get_device_identifier()})",
            "Authorization": f"Bearer {token}",
            "Accept": "*/*",
        },
        timeout=timeout,
        follow_redirects=True,
    )


def _check_cloud_error(resp: httpx.Response) -> None:
    err = resp.headers.get("X-SkyDemon-CloudStorageError")
    if err:
        raise RuntimeError(f"cloud storage error: {err}")


def list_cloud_files(token: str, pattern: str = "*.flightplan",
                     include_deleted: bool = False) -> list[CloudFile]:
    """Mirror CloudStorageListFilesJob (User root)."""
    with _client(token, LIST_TIMEOUT) as client:
        resp = client.post(
            f"/Cloud/User/ListFiles?pattern={pattern}&includedeleted={include_deleted}",
            json={"Pattern": pattern, "IncludeDeleted": include_deleted,
                  "ExtendedProperties": True},
            headers={"Content-Type": "application/json"},
        )
    if resp.status_code == 401:
        raise RuntimeError("not logged in (401) — run skydemon_login first")
    resp.raise_for_status()
    _check_cloud_error(resp)
    root = ET.fromstring(resp.content)
    if root.tag != "CloudStorageResults":
        raise RuntimeError("unexpected cloud list response")
    out = []
    for el in root:
        if el.tag == "CloudStorageFile":
            out.append(CloudFile(
                el.get("Name", ""),
                int(el.get("Size", "0")),
                _filetime_to_dt(int(el.get("LastModified", "0"))),
            ))
    return out


def _read_cs_string(buf: io.BytesIO) -> str:
    result = shift = 0
    while True:
        b = buf.read(1)
        if not b:
            raise ValueError("truncated string")
        byte = b[0]
        result |= (byte & 0x7F) << shift
        shift += 7
        if not (byte & 0x80):
            break
    data = buf.read(result)
    if len(data) != result:
        raise ValueError("truncated string bytes")
    return data.decode("utf-8")


@dataclass
class CloudDownload:
    name: str
    last_modified: datetime
    contents: bytes


def download_cloud_files(token: str, filenames: list[str]) -> list[CloudDownload]:
    """Mirror CloudStorageGetFilesJob (User root)."""
    if not filenames:
        return []
    with _client(token) as client:
        resp = client.post(
            "/Cloud/User/GetFiles?na",
            json={"Filenames": list(filenames)},
            headers={"Content-Type": "application/json"},
        )
    if resp.status_code == 401:
        raise RuntimeError("not logged in (401) — run skydemon_login first")
    resp.raise_for_status()
    _check_cloud_error(resp)
    buf = io.BytesIO(resp.content)
    out = []
    while buf.tell() < len(resp.content):
        name = _read_cs_string(buf)
        (n,) = struct.unpack("<i", buf.read(4))
        (ft,) = struct.unpack("<q", buf.read(8))
        contents = buf.read(n)
        if len(contents) != n:
            raise RuntimeError("truncated cloud download")
        out.append(CloudDownload(name, _filetime_to_dt(ft), contents))
    return out
