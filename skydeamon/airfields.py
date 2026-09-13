"""Offline airfield search + online extras (pilot notes, live feedback).

Offline index is a clean-room reimplementation of the SkyDemon chart readers
(tmp/decompiled/Divelements.Aviation.decompiled.cs):
- ChartFile.FromFile (~47943): magic 858030012, embedded files (GZip optional)
- *.AIRFIELDS.BIN via AerodromeLoader.LoadAirfieldsFromBinary (~45711)
  + SaveAirfieldsToBinary (~45877, ground-truth field order)
  + Runway/frequency/fuel binary layouts (~45842, ~46244, ~46238)

Online extras mirror AirfieldScreen (~137855, ~137869):
- PilotNotes/{position-long} and AirfieldFeedbackUI/{long}?lvt= (HTML -> text)
- position key: EarthPosition.ConvertToLong (~Divelements.Mapping:386)

Read-only: chart files are only opened for reading; nothing is written.
"""
from __future__ import annotations

import gzip
import io
import os
import struct
import threading
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path

import httpx

CONTAINER_MAGIC = 858030012
AIRFIELDS_MAGIC = 655848236

FUEL_TYPES = {0: "AvGas100LL", 1: "JetA", 2: "Mogas", 3: "AvGasUL91",
              4: "MoDiesel", 5: "Electricity", 255: "None"}
SURFACES = ["Concrete", "Asphalt", "Gravel", "Water", "Grass", "Ice",
            "Metal", "Sand", "Unknown", "Snow", "Earth", "Mixed"]
AVAILABILITY = ["Unknown", "Public", "NotUsed", "Private"]
FACILITIES = ["Civilian", "Military", "PrivateAerodrome", "Hospital", "OffshorePlatform"]


# ------------------------------------------------------------- binary -------

def _r7(buf: io.BytesIO) -> int:
    result = shift = 0
    while True:
        b = buf.read(1)
        if not b:
            raise ValueError("truncated 7-bit int")
        byte = b[0]
        result |= (byte & 0x7F) << shift
        shift += 7
        if not (byte & 0x80):
            return result


def _rstr(buf: io.BytesIO) -> str:
    n = _r7(buf)
    data = buf.read(n)
    if len(data) != n:
        raise ValueError("truncated string")
    return data.decode("utf-8")


def _rdbl(buf: io.BytesIO) -> float:
    return struct.unpack("<d", buf.read(8))[0]


def _rflt(buf: io.BytesIO) -> float:
    return struct.unpack("<f", buf.read(4))[0]


def _ri32(buf: io.BytesIO) -> int:
    return struct.unpack("<i", buf.read(4))[0]


def position_to_long(lon_deg: float, lat_deg: float) -> int:
    """Mirror of EarthPosition.ConvertToLong."""
    a = round((lon_deg + 180.0) * 3600.0)
    b = round((lat_deg + 90.0) * 3600.0)
    return a * 648000 + b


# ---------------------------------------------------------- container -------

@dataclass
class _Embedded:
    name: str
    offset: int
    length: int
    compressed: bool


def _read_container(path: Path):
    """Return (country, part, produced_filetime, [embedded]). None if not a chart."""
    try:
        with open(path, "rb") as f:
            buf = io.BytesIO(f.read())
    except OSError:
        return None
    try:
        if _ri32(buf) != CONTAINER_MAGIC:
            return None
        _ri32(buf)  # format version (>=7 assumed for installed charts)
        country = _ri32(buf)
        part = _ri32(buf)
        produced = struct.unpack("<q", buf.read(8))[0]
        buf.read(16)  # valid from/to
        _rstr(buf)  # name
        buf.read(1)  # quality
        _rstr(buf)  # category (v7+)
        _rstr(buf)  # product code (v7+)
        out = []
        while buf.tell() < len(buf.getvalue()):
            comp = buf.read(1)
            if not comp:
                break
            name = _rstr(buf)
            length = _ri32(buf)
            out.append(_Embedded(name, buf.tell(), length, comp != b"\x00"))
            buf.seek(length, io.SEEK_CUR)
        return country, part, produced, out
    except (ValueError, struct.error):
        return None


def _embedded_bytes(path: Path, emb: _Embedded) -> bytes:
    with open(path, "rb") as f:
        f.seek(emb.offset)
        data = f.read(emb.length)
    if emb.compressed:
        data = gzip.decompress(data)
    return data


# ------------------------------------------------------------ records -------

@dataclass
class RunwayInfo:
    length_m: float
    width_m: float
    surface: str
    disused: bool
    base_id: str = ""
    far_id: str = ""


@dataclass
class FreqInfo:
    designation: str
    callsign: str
    mhz: float


@dataclass
class Airfield:
    name: str
    icao: str
    lon: float
    lat: float
    elevation_ft: float
    local_id: str = ""
    availability: str = ""
    facility: str = ""
    circuits: str = ""
    telephone: str = ""
    url: str = ""
    email: str = ""
    opening_hours: str = ""
    runways: list = field(default_factory=list)
    freqs: list = field(default_factory=list)
    fuel: list = field(default_factory=list)
    chart: str = ""


def _skip_runway(buf: io.BytesIO) -> RunwayInfo:
    length, width = _rflt(buf), _rflt(buf)
    active = buf.read(1) != b"\x00"
    _rstr(buf)  # notes
    surface = buf.read(1)[0]
    buf.read(3)
    # base end
    base_id = _rstr(buf)
    buf.read(8 + 8 + 4 + 4 + 1 + 2 + 4 + 9)
    far_id = ""
    if buf.read(1) != b"\x00":
        far_id = _rstr(buf)
        buf.read(8 + 8 + 4 + 4 + 1 + 2 + 4 + 9)
    return RunwayInfo(length, width,
                      SURFACES[surface] if surface < len(SURFACES) else f"?{surface}",
                      not active, base_id, far_id)


def _parse_airfields_blob(data: bytes, chart: str) -> list[Airfield]:
    buf = io.BytesIO(data)
    if _ri32(buf) != AIRFIELDS_MAGIC:
        raise ValueError("not an AIRFIELDS.BIN blob")
    n = _ri32(buf)
    out = []
    for _ in range(n):
        _rstr(buf)  # reserved
        name = _rstr(buf)
        lon, lat = _rdbl(buf), _rdbl(buf)
        icao = _rstr(buf)
        local_id = _rstr(buf)
        elev = _rflt(buf)
        _ri32(buf)  # traffic type
        facility = _ri32(buf)
        circuits = _rstr(buf)
        _rstr(buf)  # metadata
        telephone = _rstr(buf)
        avail = buf.read(1)[0]
        url = _rstr(buf)
        email = _rstr(buf)
        opening = _rstr(buf)
        _rstr(buf); _rstr(buf); _rstr(buf)
        a = Airfield(name, icao, lon, lat, elev, local_id,
                     AVAILABILITY[avail] if avail < len(AVAILABILITY) else f"?{avail}",
                     FACILITIES[facility] if facility < len(FACILITIES) else f"?{facility}",
                     circuits, telephone, url, email, opening, chart=chart)
        for _ in range(_ri32(buf)):
            a.runways.append(_skip_runway(buf))
        for _ in range(_ri32(buf)):
            desig, call = _rstr(buf), _rstr(buf)
            mhz = _ri32(buf) / 1000.0
            _rdbl(buf)  # radius
            _rstr(buf)  # qualifier
            a.freqs.append(FreqInfo(desig, call, mhz))
        for _ in range(_ri32(buf)):
            ftype = _ri32(buf)
            comments = _rstr(buf)
            a.fuel.append({"type": FUEL_TYPES.get(ftype & 0xFF, f"?{ftype}"),
                           "comments": comments})
        tail = _ri32(buf)
        if tail == 1:
            _ri32(buf)
            s = _rstr(buf)
            if s.startswith("X"):
                buf.read(16 + 4)
            else:
                buf.read(20)
        else:
            for _ in range(tail):
                _ri32(buf); _rstr(buf); _rdbl(buf); _rdbl(buf); _ri32(buf)
        out.append(a)
    return out


# -------------------------------------------------------------- index -------

_lock = threading.Lock()
_index: list[Airfield] | None = None
_by_icao: dict[str, Airfield] = {}


def _default_chart_dirs() -> list[Path]:
    dirs = []
    override = os.environ.get("SKYDEMON_CHARTS_DIR", "").strip()
    if override:
        dirs.append(Path(override).expanduser())
    appdata = os.environ.get("APPDATA", "")
    if appdata:
        dirs.append(Path(appdata) / "Divelements Limited" / "SkyDemon Plan" / "Charts")
    install = os.environ.get("SKYDEMON_INSTALL_DIR", r"C:\Program Files (x86)\SkyDemon")
    dirs.append(Path(install) / "Data")
    return [d for d in dirs if d.is_dir()]


def _latest_charts(dirs: list[Path]) -> list[Path]:
    best: dict[tuple, tuple] = {}  # (country, part) -> (produced, path)
    for d in dirs:
        for p in sorted(d.glob("*.skydata")):
            meta = _read_container(p)
            if not meta:
                continue
            country, part, produced, _ = meta
            key = (country, part)
            if key not in best or produced > best[key][0]:
                best[key] = (produced, p)
    return [p for _, p in best.values()]


def build_index(refresh: bool = False) -> list[Airfield]:
    global _index, _by_icao
    with _lock:
        if _index is not None and not refresh:
            return _index
        fields: list[Airfield] = []
        for chart in _latest_charts(_default_chart_dirs()):
            meta = _read_container(chart)
            if not meta:
                continue
            _, _, _, embedded = meta
            for emb in embedded:
                if not emb.name.upper().endswith(".AIRFIELDS.BIN"):
                    continue
                try:
                    fields.extend(_parse_airfields_blob(
                        _embedded_bytes(chart, emb), chart.name))
                except (ValueError, struct.error, OSError, EOFError):
                    continue
        # de-dupe by ICAO (later charts win), keep nameless-position entries too
        by_icao: dict[str, Airfield] = {}
        extra: list[Airfield] = []
        for a in fields:
            if a.icao:
                by_icao[a.icao.upper()] = a
            else:
                extra.append(a)
        _index = list(by_icao.values()) + extra
        _by_icao = by_icao
        return _index


def search_airfields(query: str, limit: int = 20) -> list[Airfield]:
    q = query.strip().upper()
    if not q:
        return []
    idx = build_index()
    exact, prefix, name = [], [], []
    for a in idx:
        icao = a.icao.upper()
        if icao == q:
            exact.append(a)
        elif icao.startswith(q):
            prefix.append(a)
        elif q in a.name.upper():
            name.append(a)
    by_size = lambda a: max([r.length_m for r in a.runways] + [0])
    prefix.sort(key=by_size, reverse=True)
    name.sort(key=by_size, reverse=True)
    return (exact + prefix + name)[:max(1, limit)]


def find_airfield(icao_or_name: str) -> Airfield | None:
    build_index()
    key = icao_or_name.strip().upper()
    if key in _by_icao:
        return _by_icao[key]
    hits = search_airfields(key, 1)
    return hits[0] if hits else None


def airfield_to_dict(a: Airfield) -> dict:
    return {
        "icao": a.icao, "name": a.name, "chart": a.chart,
        "lat": round(a.lat, 6), "lon": round(a.lon, 6),
        "elevation_ft": round(a.elevation_ft, 1),
        "local_id": a.local_id, "availability": a.availability,
        "facility": a.facility, "circuits": a.circuits,
        "telephone": a.telephone, "url": a.url, "email": a.email,
        "opening_hours": a.opening_hours,
        "runways": [{"length_m": round(r.length_m, 1), "width_m": round(r.width_m, 1),
                     "surface": r.surface, "disused": r.disused,
                     "base": r.base_id, "far": r.far_id} for r in a.runways],
        "frequencies": [{"designation": f.designation, "callsign": f.callsign,
                         "mhz": round(f.mhz, 3)} for f in a.freqs],
        "fuel": a.fuel,
        "position_key": position_to_long(a.lon, a.lat),
    }


# ------------------------------------------------------- online extras -------

class _Text(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts: list[str] = []
        self._skip = False

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip = True
        elif tag in ("p", "br", "div", "li", "tr", "h1", "h2", "h3", "h4"):
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self._skip = False

    def handle_data(self, data):
        if not self._skip and data.strip():
            self.parts.append(data.strip() + " ")


def html_to_text(html: str, limit: int = 6000) -> str:
    p = _Text()
    p.feed(html)
    text = "".join(p.parts)
    text = "\n".join(line.strip() for line in text.splitlines() if line.strip())
    return text[:limit]


def fetch_online_text(api_path: str, token: str, timeout: float = 20.0) -> str:
    """Fetch a ServerPageBrowser URL the way the app does (146399-146442):
    GET Login/GetUICookie (Bearer) -> set uiguid cookie -> GET page with
    ?a=b&culture=en-GB. Direct GETs without the UI-cookie flow get 403.
    """
    import uuid as _uuid

    from .config import BASE_URL, PRODUCT_NAME, PRODUCT_VERSION
    from .api import get_device_identifier
    headers = {
        "Accept": "text/html,*/*",
        "User-Agent": f"{PRODUCT_NAME} (Version {PRODUCT_VERSION}; Device {get_device_identifier()})",
        "Authorization": f"Bearer {token}",
    }
    path = api_path.lstrip("/")
    with httpx.Client(headers=headers, timeout=timeout,
                      follow_redirects=True) as client:
        r = client.get(f"{BASE_URL}/Login/GetUICookie")
        if r.status_code == 401:
            raise RuntimeError("not logged in (401) — run skydemon_login first")
        r.raise_for_status()
        guid_text = r.text.strip().strip('"')
        try:
            _uuid.UUID(guid_text)
        except ValueError:
            raise RuntimeError("UI-cookie token corrupt") from None
        client.cookies.set("uiguid", guid_text, domain="data.skydemon.aero")
        url = f"{BASE_URL}/{path}"
        url += ("?a=b" if "?" not in url else "") + "&culture=en-GB"
        page = client.get(url)
        if page.status_code == 401:
            raise RuntimeError("not logged in (401) — run skydemon_login first")
        page.raise_for_status()
    ctype = page.headers.get("content-type", "")
    if "html" in ctype or "<html" in page.text[:500].lower():
        return html_to_text(page.text)
    return page.text[:6000]


def pilot_notes_text(a: Airfield, token: str) -> str:
    return fetch_online_text(f"PilotNotes/{position_to_long(a.lon, a.lat)}", token)


def feedback_ui_text(a: Airfield, token: str) -> str:
    return fetch_online_text(
        f"AirfieldFeedbackUI/{position_to_long(a.lon, a.lat)}?lvt=0", token)
