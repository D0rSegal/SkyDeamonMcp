import io
import struct
from datetime import datetime, timezone

import httpx
import pytest

from skydeamon import cloud as cl
from skydeamon.api import _write_cs_string

EPOCH = datetime(1601, 1, 1, tzinfo=timezone.utc)


def _ft(dt):
    return int((dt - EPOCH).total_seconds() * 10_000_000)


def _handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path.endswith("/ListFiles"):
        xml = (b'<CloudStorageResults>'
               b'<CloudStorageFile Name="EGKK-EGCC.flightplan" Size="1234" '
               b'LastModified="133000000000000000"/>'
               b'<CloudStorageFile Name="notes.gpx" Size="99" LastModified="133000000000000001"/>'
               b'</CloudStorageResults>')
        return httpx.Response(200, content=xml)
    if path.endswith("/GetFiles"):
        buf = io.BytesIO()
        _write_cs_string(buf, "EGKK-EGCC.flightplan")
        data = b"<DivelementsFlightPlanner/>"
        buf.write(struct.pack("<i", len(data)))
        buf.write(struct.pack("<q", _ft(datetime(2026, 1, 2, tzinfo=timezone.utc))))
        buf.write(data)
        return httpx.Response(200, content=buf.getvalue())
    return httpx.Response(404)


@pytest.fixture()
def mock_cloud(monkeypatch):
    orig = httpx.Client
    monkeypatch.setattr(httpx, "Client",
                        lambda **kw: orig(transport=httpx.MockTransport(_handler), **kw))


def test_list_pattern_sent(mock_cloud):
    files = cl.list_cloud_files("tok", "*.flightplan")
    assert [(f.name, f.size) for f in files] == [
        ("EGKK-EGCC.flightplan", 1234), ("notes.gpx", 99)]
    assert files[0].last_modified.year == 2022  # 133e15 filetime


def test_download_binary(mock_cloud):
    (dl,) = cl.download_cloud_files("tok", ["EGKK-EGCC.flightplan"])
    assert dl.contents == b"<DivelementsFlightPlanner/>"
    assert dl.last_modified == datetime(2026, 1, 2, tzinfo=timezone.utc)


def test_401(monkeypatch):
    orig = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda **kw: orig(
        transport=httpx.MockTransport(lambda r: httpx.Response(401)), **kw))
    with pytest.raises(RuntimeError, match="not logged in"):
        cl.list_cloud_files("bad")
