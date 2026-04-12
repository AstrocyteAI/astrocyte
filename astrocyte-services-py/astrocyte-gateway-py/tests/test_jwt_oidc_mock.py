"""OIDC auth path (JWKS) — decode mocked."""

from __future__ import annotations

from unittest import mock

import pytest
from fastapi.testclient import TestClient


def test_jwt_oidc_uses_actor_from_patch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ASTROCYTE_CONFIG_PATH", raising=False)
    monkeypatch.setenv("ASTROCYTE_AUTH_MODE", "jwt_oidc")
    monkeypatch.setenv("ASTROCYTE_OIDC_ISSUER", "https://issuer.example/")
    monkeypatch.setenv("ASTROCYTE_OIDC_AUDIENCE", "astrocyte")
    monkeypatch.setenv("ASTROCYTE_OIDC_JWKS_URL", "https://issuer.example/jwks.json")

    fake_payload = {
        "sub": "user-abc",
        "astrocyte_actor_type": "agent",
        "tid": "t1",
    }

    with mock.patch(
        "astrocyte_gateway.auth._decode_oidc_rs256",
        return_value=fake_payload,
    ):
        from astrocyte_gateway.app import create_app

        client = TestClient(create_app())
        r = client.post(
            "/v1/retain",
            json={"content": "x", "bank_id": "b1"},
            headers={"Authorization": "Bearer fake.jwt.token"},
        )
        assert r.status_code == 200
