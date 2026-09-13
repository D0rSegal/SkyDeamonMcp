import os
from pathlib import Path

from skydeamon import flightplans as fp
from skydeamon.session import clear_session, session_status


def _write_plan(tmp_path, name="EGKK-EGCC.flightplan"):
    xml = """<?xml version="1.0" encoding="utf-8"?>
<DivelementsFlightPlanner>
  <AircraftReference Name="Cessna 150M" Registration="OKISR" />
  <PrimaryRoute Start="N510850 W000138" StartType="Airfield" Level="3000 ft" Rules="VFR" CourseType="RhumbLine" Time="133000000000000000">
    <RhumbLineRoute To="N511200 W000500" ToType="UserWaypoint" Level="3000 ft" UserName="LAM" />
    <Leg To="N532100 W002100" ToType="Airfield" Level="2500 ft" />
  </PrimaryRoute>
</DivelementsFlightPlanner>"""
    p = Path(tmp_path) / name
    p.write_text(xml, encoding="utf-8")
    return p


def test_list_empty(tmp_path, monkeypatch):
    monkeypatch.setenv("SKYDEMON_ROUTES_DIR", str(tmp_path))
    assert fp.list_flightplans() == []


def test_list_and_read(tmp_path, monkeypatch):
    p = _write_plan(tmp_path)
    monkeypatch.setenv("SKYDEMON_ROUTES_DIR", str(tmp_path))
    plans = fp.list_flightplans()
    assert len(plans) == 1 and plans[0]["name"] == p.name
    s = fp.read_flightplan(p.name)
    d = fp.summary_to_dict(s)
    assert d["aircraft"] == {"name": "Cessna 150M", "registration": "OKISR"}
    assert len(d["routes"]) == 1
    assert d["routes"][0]["start"] == "N510850 W000138"
    assert [l["to"] for l in d["routes"][0]["legs"]] == ["N511200 W000500", "N532100 W002100"]
    assert d["routes"][0]["takeoff_time"] != ""


def test_read_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("SKYDEMON_ROUTES_DIR", str(tmp_path))
    try:
        fp.read_flightplan("nope.flightplan")
        assert False, "expected FileNotFoundError"
    except FileNotFoundError:
        pass


def test_session_initially_empty():
    clear_session()
    # ensure no creds leak into this check
    assert session_status() == {"logged_in": False}
