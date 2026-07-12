"""REST auth modes: dev header, API key, JWT HS256."""

from __future__ import annotations

import jwt
import pytest
from fastapi.testclient import TestClient


@pytest.fixture(autouse=True)
def _no_astrocyte_config_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ASTROCYTE_CONFIG_PATH", raising=False)


def test_dev_mode_uses_x_astrocyte_principal(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ASTROCYTE_AUTH_MODE", "dev")
    from astrocyte_gateway.app import create_app

    client = TestClient(create_app())
    r = client.post(
        "/v1/retain",
        json={"content": "x", "bank_id": "b1"},
        headers={"X-Astrocyte-Principal": "agent:dev"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body.get("stored") is True


def test_api_key_rejects_bad_key(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ASTROCYTE_AUTH_MODE", "api_key")
    monkeypatch.setenv("ASTROCYTE_API_KEY", "secret")
    from astrocyte_gateway.app import create_app

    client = TestClient(create_app())
    r = client.post(
        "/v1/retain",
        json={"content": "x", "bank_id": "b1"},
        headers={"X-Api-Key": "wrong", "X-Astrocyte-Principal": "agent:x"},
    )
    assert r.status_code == 401


def test_api_key_accepts_valid_key_and_principal(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ASTROCYTE_AUTH_MODE", "api_key")
    monkeypatch.setenv("ASTROCYTE_API_KEY", "good")
    from astrocyte_gateway.app import create_app

    client = TestClient(create_app())
    r = client.post(
        "/v1/retain",
        json={"content": "x", "bank_id": "b1"},
        headers={"X-Api-Key": "good", "X-Astrocyte-Principal": "agent:key-user"},
    )
    assert r.status_code == 200


def test_jwt_principal_from_sub(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ASTROCYTE_AUTH_MODE", "jwt_hs256")
    secret = "unit-test-secret-at-least-32-bytes-long"
    monkeypatch.setenv("ASTROCYTE_JWT_SECRET", secret)
    token = jwt.encode(
        {"sub": "agent:jwt-user"},
        secret,
        algorithm="HS256",
    )
    from astrocyte_gateway.app import create_app

    client = TestClient(create_app())
    r = client.post(
        "/v1/retain",
        json={"content": "x", "bank_id": "b1"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200


def _hs256_client(monkeypatch: pytest.MonkeyPatch) -> tuple[TestClient, str]:
    secret = "unit-test-secret-at-least-32-bytes-long"
    monkeypatch.setenv("ASTROCYTE_AUTH_MODE", "jwt_hs256")
    monkeypatch.setenv("ASTROCYTE_JWT_SECRET", secret)
    monkeypatch.setenv("ASTROCYTE_JWT_ISSUER", "https://issuer.example.com")
    from astrocyte_gateway.app import create_app

    return TestClient(create_app()), secret


def _post_retain(client: TestClient, token: str):
    return client.post(
        "/v1/retain",
        json={"content": "x", "bank_id": "b1"},
        headers={"Authorization": f"Bearer {token}"},
    )


def test_jwt_hs256_accepts_matching_issuer(monkeypatch: pytest.MonkeyPatch):
    client, secret = _hs256_client(monkeypatch)
    token = jwt.encode(
        {"sub": "agent:iss-user", "iss": "https://issuer.example.com"},
        secret,
        algorithm="HS256",
    )
    assert _post_retain(client, token).status_code == 200


def test_jwt_hs256_rejects_wrong_issuer(monkeypatch: pytest.MonkeyPatch):
    client, secret = _hs256_client(monkeypatch)
    token = jwt.encode(
        {"sub": "agent:iss-user", "iss": "https://evil.example.com"},
        secret,
        algorithm="HS256",
    )
    assert _post_retain(client, token).status_code == 401


def test_jwt_hs256_rejects_missing_issuer_when_required(monkeypatch: pytest.MonkeyPatch):
    # A token minted by another service that shares the secret but omits `iss`
    # must be rejected once ASTROCYTE_JWT_ISSUER is configured.
    client, secret = _hs256_client(monkeypatch)
    token = jwt.encode({"sub": "agent:no-iss"}, secret, algorithm="HS256")
    assert _post_retain(client, token).status_code == 401
