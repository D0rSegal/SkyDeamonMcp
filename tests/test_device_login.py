import hashlib
import io
import struct
import uuid
from datetime import datetime, timezone, timedelta

from skydeamon.api import (
    _write_cs_string,
    parse_device_login,
    split_login_slot,
    build_login_payload,
)
from skydeamon.config import (
    DEVICE_LOGIN_MAGIC,
    LICENSES_UPDATED_GUID,
    PLANNING_PRODUCT_GUID,
    get_url_for_api,
)

EPOCH = datetime(1601, 1, 1, tzinfo=timezone.utc)


def _ft(dt):
    return int((dt - EPOCH).total_seconds() * 10_000_000)


def _license_blob(token_guid, product_guid, name, valid_to):
    inner = io.BytesIO()
    inner.write(uuid.UUID(token_guid).bytes_le)
    inner.write(uuid.UUID(product_guid).bytes_le)
    _write_cs_string(inner, name)
    inner.write(struct.pack("<q", _ft(valid_to)))
    data = inner.getvalue()
    out = io.BytesIO()
    out.write(struct.pack("<i", len(data)))
    out.write(data)
    h = hashlib.sha1(data).digest()
    out.write(struct.pack("<i", len(h)))
    out.write(h)
    return out.getvalue()


def _blob():
    token = str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    buf = io.BytesIO()
    buf.write(struct.pack("<i", DEVICE_LOGIN_MAGIC))
    buf.write(_license_blob(token, PLANNING_PRODUCT_GUID, "EVAL|Test Pilot", now + timedelta(days=30)))
    buf.write(_license_blob("00000000-0000-0000-0000-000000000000", LICENSES_UPDATED_GUID, "", now))
    return buf.getvalue(), token


def test_url():
    assert get_url_for_api("Login/LoginDevice") == "https://data.skydemon.aero/Login/LoginDevice"


def test_split_slot():
    assert split_login_slot("user") == ("user", -1)
    assert split_login_slot("user/3") == ("user", 3)
    assert split_login_slot("user/x") == ("user", -1)


def test_payload():
    p = build_login_payload("user/2", "pw", device_id="P|X", device_type="T")
    assert p == {"Login": "user", "Slot": 2, "Password": "pw",
                 "DeviceIdentifier": "P|X", "DeviceType": "T"}


def test_parse_roundtrip():
    blob, token = _blob()
    res = parse_device_login(blob)
    assert res.authentication_token == token
    assert res.licensed_to == "Test Pilot"
    assert res.license_type == "Evaluation"
    assert len(res.licenses) == 1
