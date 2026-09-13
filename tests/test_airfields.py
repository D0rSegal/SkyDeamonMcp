import io
import struct

import pytest

from skydeamon.airfields import (
    _parse_airfields_blob, html_to_text, position_to_long, search_airfields,
)
from skydeamon.api import _write_cs_string
from skydeamon.airfields import AIRFIELDS_MAGIC


def _wstr(buf, s):
    _write_cs_string(buf, s)


def _record(buf, name="Test Field", icao="XTST", lon=16.5, lat=48.1):
    _wstr(buf, "")  # reserved
    _wstr(buf, name)
    buf.write(struct.pack("<d", lon))
    buf.write(struct.pack("<d", lat))
    _wstr(buf, icao)
    _wstr(buf, "")  # local id
    buf.write(struct.pack("<f", 600.0))  # elevation ft
    buf.write(struct.pack("<i", 0))  # traffic
    buf.write(struct.pack("<i", 0))  # facility
    _wstr(buf, "")  # circuits
    _wstr(buf, "")  # metadata
    _wstr(buf, "")  # telephone
    buf.write(b"\x01")  # availability Public
    _wstr(buf, ""); _wstr(buf, ""); _wstr(buf, "")
    _wstr(buf, ""); _wstr(buf, ""); _wstr(buf, "")
    # one runway
    buf.write(struct.pack("<i", 1))
    buf.write(struct.pack("<f", 1000.0))  # length m
    buf.write(struct.pack("<f", 30.0))  # width m
    buf.write(b"\x01")  # active
    _wstr(buf, "")  # notes
    buf.write(b"\x01")  # Asphalt
    buf.write(b"\x00\x00\x00")
    _wstr(buf, "09")
    buf.write(struct.pack("<d", lon))
    buf.write(struct.pack("<d", lat))
    buf.write(struct.pack("<f", 600.0))
    buf.write(struct.pack("<f", 90.0))
    buf.write(b"\x00")  # accuracy
    buf.write(struct.pack("<H", 0))
    buf.write(struct.pack("<I", 0))
    buf.write(b"\x00" * 9)
    buf.write(b"\x01")  # has far end
    _wstr(buf, "27")
    buf.write(struct.pack("<d", lon + 0.01))
    buf.write(struct.pack("<d", lat))
    buf.write(struct.pack("<f", 600.0))
    buf.write(struct.pack("<f", 270.0))
    buf.write(b"\x00")
    buf.write(struct.pack("<H", 0))
    buf.write(struct.pack("<I", 0))
    buf.write(b"\x00" * 9)
    # one frequency
    buf.write(struct.pack("<i", 1))
    _wstr(buf, "Tower"); _wstr(buf, "Test Tower")
    buf.write(struct.pack("<i", 118100))
    buf.write(struct.pack("<d", 25.0))
    _wstr(buf, "")
    # one fuel
    buf.write(struct.pack("<i", 1))
    buf.write(struct.pack("<i", 0))
    _wstr(buf, "self-service")
    buf.write(struct.pack("<i", 0))  # no mapped entities


def _blob():
    buf = io.BytesIO()
    buf.write(struct.pack("<i", AIRFIELDS_MAGIC))
    buf.write(struct.pack("<i", 1))
    _record(buf)
    return buf.getvalue()


def test_parse_synthetic():
    (a,) = _parse_airfields_blob(_blob(), "test")
    assert (a.name, a.icao) == ("Test Field", "XTST")
    assert abs(a.lon - 16.5) < 1e-9 and abs(a.lat - 48.1) < 1e-9
    assert a.elevation_ft == pytest.approx(600.0)
    assert a.availability == "Public"
    (r,) = a.runways
    assert (r.length_m, r.width_m, r.surface) == (pytest.approx(1000.0), pytest.approx(30.0), "Asphalt")
    assert (r.base_id, r.far_id, r.disused) == ("09", "27", False)
    assert [(f.designation, f.mhz) for f in a.freqs] == [("Tower", 118.1)]
    assert a.fuel == [{"type": "AvGas100LL", "comments": "self-service"}]


def test_position_key():
    # LoWW approx: lon 16.5697, lat 48.1103
    key = position_to_long(16.5697, 48.1103)
    assert key == round((16.5697 + 180) * 3600) * 648000 + round((48.1103 + 90) * 3600)


def test_html_to_text():
    html = "<html><body><h1>Notes</h1><p>PPR by phone.</p><script>x=1</script></body></html>"
    t = html_to_text(html)
    assert "PPR by phone" in t and "x=1" not in t


def test_ui_cookie_flow(monkeypatch):
    """ServerPageBrowser flow without network: GetUICookie -> uiguid cookie -> page."""
    import httpx
    from skydeamon import airfields as af

    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/Login/GetUICookie"):
            return httpx.Response(200, headers={"Content-Type": "application/json",
                                                "Set-Cookie": "sess=abc; Path=/"},
                                  content=b'"11111111-2222-3333-4444-555555555555"')
        seen["cookie"] = request.headers.get("cookie", "")
        seen["url"] = str(request.url)
        return httpx.Response(200, headers={"Content-Type": "text/html"},
                              content=b"<html><body><p>Hello</p></body></html>")

    _OrigClient = httpx.Client
    monkeypatch.setattr(httpx, "Client",
                        lambda **kw: _OrigClient(transport=httpx.MockTransport(handler), **kw))
    text = af.fetch_online_text("PilotNotes/123", "tok")
    assert "Hello" in text
    assert "uiguid=11111111-2222-3333-4444-555555555555" in seen["cookie"]
    assert "culture=en-GB" in seen["url"]


def test_search_real_charts():
    from pathlib import Path
    import os
    appdata = os.environ.get("APPDATA", "")
    if not Path(appdata, "Divelements Limited", "SkyDemon Plan", "Charts").is_dir():
        pytest.skip("no charts installed")
    hits = search_airfields("LOWW")
    assert hits and hits[0].icao == "LOWW"
    assert max(r.length_m for r in hits[0].runways) == pytest.approx(3600.0)
