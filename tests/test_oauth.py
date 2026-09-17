import base64
import hashlib
import pytest
from urllib.parse import parse_qs, urlparse
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
    assert "code_challenge_methods_supported" in data
    assert "S256" in data["code_challenge_methods_supported"]

    # Test OpenID config
    res2 = client.get("/.well-known/openid-configuration")
    assert res2.status_code == 200

    # Test RFC 9728 Protected Resource
    res3 = client.get("/.well-known/oauth-protected-resource")
    assert res3.status_code == 200
    prm = res3.json()
    assert prm["resource"].endswith("/mcp")
    assert "authorization_servers" in prm


def test_oauth_authorize_page_and_approval_with_pkce_and_state():
    app = server.build_http_app()
    client = TestClient(app)

    # Prepare PKCE S256 challenge
    code_verifier = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    code_challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")

    complex_state = "APrAeJFqfWxYpTD_5BBa4V9eu-H9eh536z4ts7Jy0GTHbEmFP6r0pqKHe68aISqYzDVJ"
    redirect_uri = "https://oauth-redirect.googleusercontent.com/r/user_bound_custom-mcp-000000000000000000000-test"

    # GET authorization page
    get_url = f"/oauth/authorize?response_type=code&client_id=gemini-spark&redirect_uri={redirect_uri}&state={complex_state}&code_challenge={code_challenge}&code_challenge_method=S256"
    res = client.get(get_url)
    assert res.status_code == 200
    assert "Authorize SkyDemon MCP" in res.text
    # Verify hidden form fields exist to avoid state loss
    assert f'name="state" value="{complex_state}"' in res.text
    assert f'name="code_challenge" value="{code_challenge}"' in res.text

    # POST approval with hidden form data
    post_res = client.post(
        "/oauth/authorize",
        data={
            "action": "approve",
            "client_id": "gemini-spark",
            "redirect_uri": redirect_uri,
            "state": complex_state,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        },
        follow_redirects=False,
    )
    assert post_res.status_code == 302
    location = post_res.headers["location"]
    assert location.startswith(redirect_uri)
    
    parsed = urlparse(location)
    qs = parse_qs(parsed.query)
    assert "code" in qs
    assert qs["state"][0] == complex_state
    code = qs["code"][0]

    # Test invalid code_verifier -> 400
    bad_verifier_res = client.post("/oauth/token", data={
        "grant_type": "authorization_code",
        "code": code,
        "client_id": "gemini-spark",
        "redirect_uri": redirect_uri,
        "code_verifier": "wrong_verifier",
    })
    # Auth code should not match or fail verification
    assert bad_verifier_res.status_code == 400

    # Get a fresh code for valid exchange
    post_res2 = client.post(
        "/oauth/authorize",
        data={
            "action": "approve",
            "client_id": "gemini-spark",
            "redirect_uri": redirect_uri,
            "state": complex_state,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        },
        follow_redirects=False,
    )
    code2 = parse_qs(urlparse(post_res2.headers["location"]).query)["code"][0]

    # Exchange code for token with correct code_verifier
    token_res = client.post("/oauth/token", data={
        "grant_type": "authorization_code",
        "code": code2,
        "client_id": "gemini-spark",
        "redirect_uri": redirect_uri,
        "code_verifier": code_verifier,
    })
    assert token_res.status_code == 200
    token_data = token_res.json()
    assert "access_token" in token_data
    assert "refresh_token" in token_data
    access_token = token_data["access_token"]
    refresh_token = token_data["refresh_token"]

    # Test refresh token exchange
    refresh_res = client.post("/oauth/token", data={
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
    })
    assert refresh_res.status_code == 200
    assert "access_token" in refresh_res.json()

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

    with TestClient(app) as client_with_lifespan:
        # Request without token -> 401 Unauthorized with RFC 9728 WWW-Authenticate
        unauth_res = client_with_lifespan.post("/mcp", json=init_payload, headers={"Accept": "application/json, text/event-stream"})
        assert unauth_res.status_code == 401
        www_auth = unauth_res.headers.get("www-authenticate", "")
        assert 'resource_metadata=' in www_auth

        # Request with Bearer token -> 200 OK
        auth_res = client_with_lifespan.post(
            "/mcp",
            json=init_payload,
            headers={
                "Accept": "application/json, text/event-stream",
                "Authorization": f"Bearer {access_token}",
            },
        )
        assert auth_res.status_code == 200
