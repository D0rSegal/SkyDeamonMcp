import io
import struct
import zlib
from datetime import datetime, timezone

import httpx
import pytest

from skydeamon import weather as wx
from skydeamon.api import _write_cs_string


def test_icao_codec():
    for code in ("LOWW", "EDDM", "KJFK", "A1B2"):
        assert wx.unpack_icao(wx.pack_icao(code)) == code
    with pytest.raises(ValueError):
        wx.pack_icao("LOW")
    with pytest.raises(ValueError):
        wx.pack_icao("low!")
    assert wx.is_icao("loww") and not wx.is_icao("Wien-Schwechat")


def _pkg_response():
    """Minimal server package: 1 METAR + 1 TAF for LOWW."""
    from skydeamon.weather import PACKAGE_MAGIC
    buf = io.BytesIO()
    buf.write(struct.pack("<i", PACKAGE_MAGIC))
    buf.write(struct.pack("<i", 0))  # length prefix (unchecked)
    buf.write(struct.pack("<H", 0))  # null metar
    buf.write(struct.pack("<H", 0))  # null taf
    buf.write(struct.pack("<H", 1))  # metars
    _write_cs_string(buf, "2026/09/13 12:00\r\nLOWW 131220Z 32008KT 9999 FEW045 18/09 Q1013")
    buf.write(struct.pack("<H", 1))  # tafs
    _write_cs_string(buf, "2026/09/13 11:00\r\nTAF LOWW 131100Z 1312/1412 32010KT 9999 SCT040")
    return buf.getvalue()


def _envelope(payload: bytes) -> bytes:
    env = io.BytesIO()
    env.write(struct.pack("<i", wx.ENVELOPE_MAGIC))
    env.write(bytes((wx.TAFMETAR_PACKAGE_TYPE,)))
    env.write(struct.pack("<i", len(payload)))
    env.write(payload)
    return zlib.compress(env.getvalue())[2:-4]  # raw deflate


def test_request_envelope_shape():
    body = wx.build_request(["LOWW", "loww", "EDDM", "XX"])
    assert body[:4] == struct.pack("<i", wx.ENVELOPE_MAGIC)
    assert body[4] == 0
    (n,) = struct.unpack("<i", body[5:9])
    assert len(body) == 9 + n
    inner = body[9:]
    (count,) = struct.unpack("<i", inner[:4])
    assert count == 2  # deduped + XX dropped
    assert wx.unpack_icao(inner[4:7]) == "LOWW"
    assert inner[7:13] == b"\x00" * 6  # zero cached minutes


def test_parse_roundtrip():
    out = wx.parse_response(_envelope(_pkg_response()))
    w = out["LOWW"]
    assert w.metar.raw.startswith("LOWW 131220Z")
    assert w.metar.time == datetime(2026, 9, 13, 12, 0, tzinfo=timezone.utc)
    assert w.taf.raw.startswith("TAF LOWW")
    assert w.taf.time == datetime(2026, 9, 13, 11, 0, tzinfo=timezone.utc)
    d = wx.weather_to_dict(w, "metar")
    assert set(d) == {"icao", "metar", "metar_missing"}
    assert d["metar_missing"] is False


def test_get_weather_mock(monkeypatch):
    orig = httpx.Client
    blob = _envelope(_pkg_response())
    monkeypatch.setattr(httpx, "Client", lambda **kw: orig(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, content=blob)), **kw))
    wx._cache.clear()
    out = wx.get_weather("tok", ["LOWW"])
    assert out["LOWW"].metar.raw.startswith("LOWW 131220Z")
    # second call served from cache (transport would fail if hit)
    monkeypatch.setattr(httpx, "Client", lambda **kw: orig(
        transport=httpx.MockTransport(lambda r: (_ for _ in ()).throw(AssertionError("net"))),
        **kw))
    out2 = wx.get_weather("tok", ["LOWW"])
    assert out2["LOWW"].metar.raw.startswith("LOWW 131220Z")
