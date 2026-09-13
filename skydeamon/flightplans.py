"""Read-only flightplan access from disk. No writes, no edits.

SkyDemon stores plans in Documents/SkyDemon/Routes as .flightplan (XML,
root DivelementsFlightPlanner — see FlightplanFile in
tmp/decompiled/SkyDemon.decompiled.cs:84491) plus .gpx.
"""
from __future__ import annotations

import os
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path


def resolve_routes_dir() -> Path:
    override = os.environ.get("SKYDEMON_ROUTES_DIR", "").strip()
    if override:
        return Path(override).expanduser()
    docs = Path(os.path.expanduser("~/Documents"))
    # Windows Personal folder can differ; keep simple + overridable
    return docs / "SkyDemon" / "Routes"


def _filetime_to_dt(ft: int) -> datetime:
    epoch = datetime(1601, 1, 1, tzinfo=timezone.utc)
    return epoch + timedelta(microseconds=ft // 10)


@dataclass
class Leg:
    kind: str
    to: str = ""
    to_type: str = ""
    level: str = ""
    level_change: str = ""
    user_name: str = ""


@dataclass
class Route:
    tag: str  # PrimaryRoute | Route
    start: str = ""
    start_type: str = ""
    level: str = ""
    takeoff_time: str = ""
    rules: str = ""
    course_type: str = ""
    legs: list = field(default_factory=list)


@dataclass
class FlightplanSummary:
    file: str
    format: str  # flightplan | gpx
    aircraft_name: str = ""
    aircraft_registration: str = ""
    routes: list = field(default_factory=list)
    gpx_waypoints: list = field(default_factory=list)


def list_flightplans(routes_dir: Path | None = None) -> list[dict]:
    d = Path(routes_dir) if routes_dir else resolve_routes_dir()
    if not d.is_dir():
        return []
    out = []
    for p in sorted(d.glob("*")):
        if p.suffix.lower() not in (".flightplan", ".gpx") or not p.is_file():
            continue
        try:
            st = p.stat()
            out.append({"name": p.name, "path": str(p),
                        "size": st.st_size, "modified": st.st_mtime})
        except OSError:
            continue
    return out


def _parse_flightplan_xml(path: Path) -> FlightplanSummary:
    tree = ET.parse(path)  # read-only parse
    return summarize_flightplan_root(tree.getroot(), str(path))


def summarize_flightplan_bytes(data: bytes, filename: str) -> FlightplanSummary:
    """Summarize downloaded (cloud) plan bytes without touching disk."""
    root = ET.fromstring(data)
    s = summarize_flightplan_root(root, filename) if root.tag in (
        "DivelementsFlightPlanner", "SkyAngel") else None
    if s is not None:
        return s
    # fall back to GPX shape
    s = FlightplanSummary(file=filename, format="gpx")
    for el in root.iter():
        tag = el.tag.rsplit("}", 1)[-1]
        if tag in ("wpt", "rtept", "trkpt"):
            s.gpx_waypoints.append({
                "kind": tag,
                "lat": el.get("lat", ""),
                "lon": el.get("lon", ""),
                "name": (el.findtext("{*}name") or "").strip(),
            })
    if not s.gpx_waypoints:
        raise ValueError(f"unrecognized plan content in {filename!r}")
    return s


def summarize_flightplan_root(root: ET.Element, filename: str) -> FlightplanSummary:
    if root.tag not in ("DivelementsFlightPlanner", "SkyAngel"):
        raise ValueError(f"unexpected root {root.tag!r}")
    s = FlightplanSummary(file=filename, format="flightplan")
    for child in root:
        if child.tag == "AircraftReference":
            s.aircraft_name = child.get("Name", "")
            s.aircraft_registration = child.get("Registration", "")
        elif child.tag == "Aircraft":
            s.aircraft_name = child.get("Name", s.aircraft_name)
            s.aircraft_registration = child.get("Registration", s.aircraft_registration)
        elif child.tag in ("PrimaryRoute", "Route"):
            r = Route(tag=child.tag,
                      start=child.get("Start", ""),
                      start_type=child.get("StartType", ""),
                      level=child.get("Level", ""),
                      rules=child.get("Rules", ""),
                      course_type=child.get("CourseType", ""))
            t = child.get("Time")
            if t:
                try:
                    r.takeoff_time = _filetime_to_dt(int(t)).isoformat()
                except (ValueError, OverflowError):
                    r.takeoff_time = ""
            for leg in child:
                if leg.tag in ("RhumbLineRoute", "Alternate", "Leg"):
                    r.legs.append(Leg(
                        kind=leg.tag,
                        to=leg.get("To", ""),
                        to_type=leg.get("ToType", ""),
                        level=leg.get("Level", ""),
                        level_change=leg.get("LevelChange", ""),
                        user_name=leg.get("UserName", ""),
                    ))
            s.routes.append(r)
    return s


def _parse_gpx(path: Path) -> FlightplanSummary:
    tree = ET.parse(path)
    s = FlightplanSummary(file=str(path), format="gpx")
    # GPX may use namespaces — match by local name
    for el in tree.getroot().iter():
        tag = el.tag.rsplit("}", 1)[-1]
        if tag in ("wpt", "rtept", "trkpt"):
            s.gpx_waypoints.append({
                "kind": tag,
                "lat": el.get("lat", ""),
                "lon": el.get("lon", ""),
                "name": (el.findtext("{*}name") or "").strip(),
            })
    return s


def read_flightplan(name_or_path: str) -> FlightplanSummary:
    """Read + summarize one plan. No edits — file is only opened for reading."""
    p = Path(name_or_path)
    if not p.is_absolute():
        p = resolve_routes_dir() / p.name
    if p.suffix.lower() == ".flightplan":
        return _parse_flightplan_xml(p)
    if p.suffix.lower() == ".gpx":
        return _parse_gpx(p)
    raise ValueError("expected .flightplan or .gpx")


def summary_to_dict(s: FlightplanSummary) -> dict:
    return {
        "file": s.file,
        "format": s.format,
        "aircraft": {"name": s.aircraft_name, "registration": s.aircraft_registration},
        "routes": [
            {"tag": r.tag, "start": r.start, "start_type": r.start_type,
             "level": r.level, "takeoff_time": r.takeoff_time,
             "rules": r.rules, "course_type": r.course_type,
             "legs": [l.__dict__ for l in r.legs]}
            for r in s.routes
        ],
        "gpx_waypoints": s.gpx_waypoints,
    }
