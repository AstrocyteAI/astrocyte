"""REST auth modes: dev header, API key, JWT HS256."""

from __future__ import annotations

import jwt
import pytest
from fastapi.testclient import TestClient


@pytest.fixture(autouse=True)
def _no_astrocytes_config_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ASTROCYTES_CONFIG_PATH", raising=False)


def test_dev_mode_uses_x_astrocytes_principal(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ASTROCYTES_AUTH_MODE", "dev")
    from astrocytes_rest.app import create_app

    client = TestClient(create_app())
    r = client.post(
        "/v1/retain",
        json={"content": "x", "bank_id": "b1"},
        headers={"X-Astrocytes-Principal": "agent:dev"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body.get("stored") is True


def test_api_key_rejects_bad_key(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ASTROCYTES_AUTH_MODE", "api_key")
    monkeypatch.setenv("ASTROCYTES_API_KEY", "secret")
    from astrocytes_rest.app import create_app

    client = TestClient(create_app())
    r = client.post(
        "/v1/retain",
        json={"content": "x", "bank_id": "b1"},
        headers={"X-Api-Key": "wrong", "X-Astrocytes-Principal": "agent:x"},
    )
    assert r.status_code == 401


def test_api_key_accepts_valid_key_and_principal(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ASTROCYTES_AUTH_MODE", "api_key")
    monkeypatch.setenv("ASTROCYTES_API_KEY", "good")
    from astrocytes_rest.app import create_app

    client = TestClient(create_app())
    r = client.post(
        "/v1/retain",
        json={"content": "x", "bank_id": "b1"},
        headers={"X-Api-Key": "good", "X-Astrocytes-Principal": "agent:key-user"},
    )
    assert r.status_code == 200


def test_jwt_principal_from_sub(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ASTROCYTES_AUTH_MODE", "jwt_hs256")
    secret = "unit-test-secret-at-least-32-bytes-long"
    monkeypatch.setenv("ASTROCYTES_JWT_SECRET", secret)
    token = jwt.encode(
        {"sub": "agent:jwt-user"},
        secret,
        algorithm="HS256",
    )
    from astrocytes_rest.app import create_app

    client = TestClient(create_app())
    r = client.post(
        "/v1/retain",
        json={"content": "x", "bank_id": "b1"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200
