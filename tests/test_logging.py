import logging
from pathlib import Path
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from skydeamon.logging_config import RequestLoggingMiddleware, log_tool_call, setup_logging, sanitize_value


def test_sanitize_value():
    assert sanitize_value("password", "supersecret") == "***REDACTED***"
    assert sanitize_value("client_secret", "12345") == "***REDACTED***"
    assert sanitize_value("normal_arg", "hello") == "hello"
    nested = {"user": "alice", "auth_token": "secret_token"}
    sanitized = sanitize_value("payload", nested)
    assert sanitized["user"] == "alice"
    assert sanitized["auth_token"] == "***REDACTED***"


def test_log_tool_call_decorator(caplog):
    @log_tool_call
    def dummy_tool(icao: str, password: str = "") -> dict:
        return {"ok": True, "count": 5}

    with caplog.at_level(logging.INFO):
        res = dummy_tool("EGLL", password="mypassword")
        assert res["ok"] is True
        assert res["count"] == 5

    records = [r.message for r in caplog.records]
    assert any("[tool:start] dummy_tool" in m for m in records)
    assert any("***REDACTED***" in m for m in records)
    assert not any("mypassword" in m for m in records)
    assert any("[tool:done] dummy_tool -> ok, count=5" in m for m in records)


def test_request_logging_middleware(caplog):
    async def hello_endpoint(request):
        return JSONResponse({"status": "healthy"})

    routes = [Route("/health", endpoint=hello_endpoint, methods=["GET"])]
    app = Starlette(routes=routes, middleware=[Middleware(RequestLoggingMiddleware)])
    client = TestClient(app)

    with caplog.at_level(logging.INFO):
        resp = client.get("/health")
        assert resp.status_code == 200

    records = [r.message for r in caplog.records]
    assert any("[http] GET /health -> 200" in m for m in records)
