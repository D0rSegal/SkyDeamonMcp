import pytest
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.testclient import TestClient

from skydeamon import auth, server


def test_oauth_metadata_discovery():
    app = server.build_http_app()
    client = TestClient(app)

    # Test RFC 8414
    res = client.get("/.well-known/oauth-authorization-server")
    assert res.status_code == 200
    data = res.json()
    assert "authorization_endpoint" in data
    assert "token_endpoint" in data
    assert data["token_endpoint"].endswith("/oauth/token")
    assert data["authorization_endpoint"].endswith("/oauth/authorize")

    # Test OpenID config
    res2 = client.get("/.well-known/openid-configuration")
    assert res2.status_code == 200


def test_oauth_authorize_page_and_approval():
    app = server.build_http_app()
    client = TestClient(app)

    # GET authorization page
    res = client.get("/oauth/authorize?client_id=gemini-spark&redirect_uri=https://gemini.google.com/callback&state=xyz123")
    assert res.status_code == 200
    assert "Authorize SkyDemon MCP" in res.text

    # POST approval
    post_res = client.post(
        "/oauth/authorize?client_id=gemini-spark&redirect_uri=https://gemini.google.com/callback&state=xyz123",
        data={"action": "approve"},
        follow_redirects=False,
    )
    assert post_res.status_code == 302
    location = post_res.headers["location"]
    assert "https://gemini.google.com/callback" in location
    assert "code=" in location
    assert "state=xyz123" in location

    # Extract code
    code = location.split("code=")[1].split("&")[0]

    # Exchange code for token
    token_res = client.post("/oauth/token", data={
        "grant_type": "authorization_code",
        "code": code,
        "client_id": "gemini-spark",
        "client_secret": "skydemon-secret-2026",
    })
    assert token_res.status_code == 200
    token_data = token_res.json()
    assert "access_token" in token_data
    access_token = token_data["access_token"]

    # Verify protected route access with Bearer token
    init_payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "gemini", "version": "1.0"},
        },
    }

    # Request with context manager to run ASGI lifespan
    with TestClient(app) as client_with_lifespan:
        # Request without token -> 401 Unauthorized
        unauth_res = client_with_lifespan.post("/mcp", json=init_payload, headers={"Accept": "application/json, text/event-stream"})
        assert unauth_res.status_code == 401

        # Request with Bearer token -> success
        auth_res = client_with_lifespan.post(
            "/mcp",
            json=init_payload,
            headers={
                "Accept": "application/json, text/event-stream",
                "Authorization": f"Bearer {access_token}",
            },
        )
        assert auth_res.status_code == 200

