"""Airfield weather (METAR/TAF) via SkyDemon's bulletin API.

Clean-room reimplementation of (tmp/decompiled/SkyDemon.decompiled.cs):
- BulletinDownloadManager.PerformDownloadRequest (~155046): envelope magic
  1945621665, per-agent {byte type, int32 len, payload}, POST Bulletin/Refresh
- TafMetarDownloadAgent (RequestPackageType 0, ~160678): payload = int32 count,
  then per ICAO {3B packed code, 3B-BE cached-METAR minutes, 3B-BE cached-TAF
  minutes} since Metar.Datum2 = 2016-01-01 UTC; 5-min client cache
- TafMetarPackage.Deserialize (~159868): magic 226435133, null-METAR/null-TAF
  ICAO lists, then METAR/TAF C# strings "yyyy/MM/dd HH:mm\\r\\nRAW"
- IcaoEntity 6-bit packing (~Divelements.Aviation:22118): A-Z=0-25, 0-9=26-36

Response bodies are raw DEFLATE (InternetConnectivity.UncompressDataZipFormat).
Only raw bulletins + timestamps are returned — the calling agent interprets
VFR/MVFR/IFR from the raw METAR text.
"""
from __future__ import annotations

import io
import re
import struct
import threading
import zlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import httpx

ENVELOPE_MAGIC = 1945621665
PACKAGE_MAGIC = 226435133
TAFMETAR_PACKAGE_TYPE = 0
DATUM2 = datetime(2016, 1, 1, tzinfo=timezone.utc)
CACHE_TTL = timedelta(minutes=5)
_ICAO_RE = re.compile(r"^[A-Z0-9]{4}$")


# ---------------------------------------------------------------- codec -----

def _char_to_byte(c: str) -> int:
    o = ord(c)
    if 65 <= o <= 90:
        return o - 65
    if 48 <= o <= 57:
        return o - 22  # '0' (48) -> 26
    raise ValueError(f"bad ICAO char {c!r}")


def _byte_to_char(b: int) -> str:
    if 0 <= b <= 25:
        return chr(b + 65)
    if 26 <= b <= 36:
        return chr(b + 22)
    return "?"


def is_icao(s: str) -> bool:
    return bool(_ICAO_RE.match(s.strip().upper()))


def pack_icao(icao: str) -> bytes:
    code = icao.strip().upper()
    if not _ICAO_RE.match(code):
        raise ValueError(f"need 4-char ICAO, got {icao!r}")
    n = (_char_to_byte(code[0]) | _char_to_byte(code[1]) << 6
         | _char_to_byte(code[2]) << 12 | _char_to_byte(code[3]) << 18)
    return bytes((n & 0xFF, (n >> 8) & 0xFF, (n >> 16) & 0xFF))


def unpack_icao(data: bytes) -> str:
    n = data[0] | data[1] << 8 | data[2] << 16
    return "".join(_byte_to_char((n >> s) & 0x3F) for s in (0, 6, 12, 18))


def _pack_minutes_be(minutes: int) -> bytes:
    minutes %= 16777216
    return bytes((minutes // 65536, (minutes % 65536) // 256, minutes % 256))


def _r7(buf: io.BytesIO) -> int:
    result = shift = 0
    while True:
        b = buf.read(1)
        if not b:
            raise ValueError("truncated string")
        byte = b[0]
        result |= (byte & 0x7F) << shift
        shift += 7
        if not (byte & 0x80):
            return result


def _rstr(buf: io.BytesIO) -> str:
    n = _r7(buf)
    data = buf.read(n)
    if len(data) != n:
        raise ValueError("truncated string bytes")
    return data.decode("utf-8")


# ---------------------------------------------------------------- model -----

@dataclass
class Bulletin:
    time: datetime
    raw: str


@dataclass
class AirfieldWeather:
    icao: str
    metar: Bulletin | None = None
    taf: Bulletin | None = None
    metar_missing: bool = False
    taf_missing: bool = False


def _split_dated_blob(s: str) -> tuple[datetime, str]:
    head, _, raw = s.partition("\r\n")
    if not raw:
        head, _, raw = s.partition("\n")
    try:
        dt = datetime.strptime(head.strip(), "%Y/%m/%d %H:%M").replace(tzinfo=timezone.utc)
    except ValueError:
        dt = datetime.min.replace(tzinfo=timezone.utc)
    return dt, raw.strip()


def build_request(icaos: list[str]) -> bytes:
    """Envelope for Bulletin/Refresh carrying one TafMetar (type 0) package."""
    codes = []
    for icao in icaos:
        code = icao.strip().upper()
        if _ICAO_RE.match(code) and code not in codes:
            codes.append(code)
    if not codes:
        raise ValueError("no valid 4-char ICAOs")
    payload = io.BytesIO()
    payload.write(struct.pack("<i", len(codes)))
    for code in codes:
        payload.write(pack_icao(code))
        payload.write(b"\x00\x00\x00")  # no cached METAR
        payload.write(b"\x00\x00\x00")  # no cached TAF
    body = payload.getvalue()
    env = io.BytesIO()
    env.write(struct.pack("<i", ENVELOPE_MAGIC))
    env.write(bytes((TAFMETAR_PACKAGE_TYPE,)))
    env.write(struct.pack("<i", len(body)))
    env.write(body)
    return env.getvalue()


def parse_response(blob: bytes) -> dict[str, AirfieldWeather]:
    """Decompress + parse envelope; returns weather keyed by ICAO."""
    try:
        raw = zlib.decompress(blob, -15)
    except zlib.error as e:
        raise ValueError(f"not a deflate bulletin: {e}") from e
    buf = io.BytesIO(raw)
    if struct.unpack("<i", buf.read(4))[0] != ENVELOPE_MAGIC:
        raise ValueError("bad bulletin envelope magic")
    out: dict[str, AirfieldWeather] = {}
    while buf.tell() < len(raw):
        ptype = buf.read(1)
        if not ptype:
            break
        (n,) = struct.unpack("<i", buf.read(4))
        payload = buf.read(n)
        if len(payload) != n or ptype[0] != TAFMETAR_PACKAGE_TYPE:
            continue
        pbuf = io.BytesIO(payload)
        if struct.unpack("<i", pbuf.read(4))[0] != PACKAGE_MAGIC:
            raise ValueError("bad TafMetar package magic")
        pbuf.read(4)  # total length prefix
        for is_taf in (False, True):
            (count,) = struct.unpack("<H", pbuf.read(2))
            for _ in range(count):
                code = unpack_icao(pbuf.read(3))
                out.setdefault(code, AirfieldWeather(code))
                if is_taf:
                    out[code].taf_missing = True
                else:
                    out[code].metar_missing = True
        for is_taf in (False, True):
            (count,) = struct.unpack("<H", pbuf.read(2))
            for _ in range(count):
                dt, raw_text = _split_dated_blob(_rstr(pbuf))
                code = next((t.strip().upper() for t in raw_text.split()[:3]
                             if _ICAO_RE.match(t.strip().upper())), "")
                if not code:
                    continue
                w = out.setdefault(code, AirfieldWeather(code))
                b = Bulletin(dt, raw_text)
                if is_taf:
                    w.taf, w.taf_missing = b, False
                else:
                    w.metar, w.metar_missing = b, False
    return out


# ---------------------------------------------------------------- client ----

_lock = threading.Lock()
_cache: dict[str, tuple[datetime, AirfieldWeather]] = {}


def _client(token: str) -> httpx.Client:
    from .config import BASE_URL, PRODUCT_NAME, PRODUCT_VERSION
    from .api import get_device_identifier
    return httpx.Client(
        base_url=BASE_URL,
        headers={
            "User-Agent": f"{PRODUCT_NAME} (Version {PRODUCT_VERSION}; "
                          f"Device {get_device_identifier()})",
            "Authorization": f"Bearer {token}",
            "Accept": "application/x-skydemon-weatherbulletins",
            "Content-Type": "application/octet-stream",
        },
        timeout=30.0, follow_redirects=True)


def get_weather(token: str, icaos: list[str],
                max_age: timedelta = CACHE_TTL) -> dict[str, AirfieldWeather]:
    """METAR+TAF for ICAOs; serves fresh cache hits, requests the rest."""
    wanted = [i.strip().upper() for i in icaos if _ICAO_RE.match(i.strip().upper())]
    if not wanted:
        raise ValueError("no valid 4-char ICAOs")
    now = datetime.now(timezone.utc)
    with _lock:
        fresh = {c: w for c in wanted
                 if (hit := _cache.get(c)) and now - hit[0] < max_age
                 for w in (hit[1],)}
    missing = [c for c in wanted if c not in fresh]
    fetched: dict[str, AirfieldWeather] = {}
    if missing:
        with _client(token) as client:
            resp = client.post("/Bulletin/Refresh", content=build_request(missing))
        if resp.status_code == 401:
            raise RuntimeError("not logged in (401) — run skydemon_login first")
        resp.raise_for_status()
        fetched = parse_response(resp.content)
        with _lock:
            for code, w in fetched.items():
                _cache[code] = (now, w)
    return {c: fetched.get(c, fresh.get(c, AirfieldWeather(c))) for c in wanted}


def weather_to_dict(w: AirfieldWeather, what: str = "both") -> dict:
    out: dict = {"icao": w.icao}
    if what in ("metar", "both"):
        out["metar"] = ({"observed": w.metar.time.isoformat(), "raw": w.metar.raw}
                        if w.metar else None)
        out["metar_missing"] = w.metar_missing and w.metar is None
    if what in ("taf", "both"):
        out["taf"] = ({"forecast": w.taf.time.isoformat(), "raw": w.taf.raw}
                      if w.taf else None)
        out["taf_missing"] = w.taf_missing and w.taf is None
    return out
