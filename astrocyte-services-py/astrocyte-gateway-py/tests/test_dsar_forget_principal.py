"""Tests for ``POST /v1/dsar/forget_principal`` — Cerebro DSAR right-to-erasure.

The endpoint sweeps configured banks matching ``{tenant_id}:*`` and deletes
memories tagged ``principal:{principal}``. Memories without that tag are
intentionally left alone (the contract requires callers to tag at retain).
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(autouse=True)
def _dev_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ASTROCYTE_AUTH_MODE", "dev")
    monkeypatch.delenv("ASTROCYTE_CONFIG_PATH", raising=False)


def _build_client():
    from astrocyte_gateway.app import create_app
    from astrocyte_gateway.brain import build_astrocyte

    brain = build_astrocyte()
    app = create_app(brain)
    return TestClient(app), brain


def test_rejects_missing_tenant_id() -> None:
    client, _ = _build_client()
    with client:
        resp = client.post("/v1/dsar/forget_principal", json={"principal": "user:alice"})
    assert resp.status_code == 400
    assert "tenant_id" in resp.json()["detail"]


def test_rejects_missing_principal() -> None:
    client, _ = _build_client()
    with client:
        resp = client.post("/v1/dsar/forget_principal", json={"tenant_id": "acme"})
    assert resp.status_code == 400
    assert "principal" in resp.json()["detail"]


def test_rejects_empty_strings() -> None:
    client, _ = _build_client()
    with client:
        resp = client.post(
            "/v1/dsar/forget_principal",
            json={"tenant_id": "", "principal": "user:alice"},
        )
    assert resp.status_code == 400


def test_returns_zero_when_no_banks_match_tenant_prefix() -> None:
    """A tenant with no configured banks is a valid no-op — returns 0 deleted."""
    client, _ = _build_client()
    with client:
        resp = client.post(
            "/v1/dsar/forget_principal",
            json={"tenant_id": "tenant-with-no-banks", "principal": "user:alice"},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["tenant_id"] == "tenant-with-no-banks"
    assert body["principal"] == "user:alice"
    assert body["tag_convention"] == "principal:user:alice"
    assert body["banks_processed"] == 0
    assert body["memories_deleted"] == 0
    assert body["details"] == []


def test_response_shape_matches_cerebro_contract() -> None:
    """Every key the Cerebro DSAR.DeletionWorker expects is present."""
    client, _ = _build_client()
    with client:
        resp = client.post(
            "/v1/dsar/forget_principal",
            json={"tenant_id": "anything", "principal": "user:bob"},
        )
    assert resp.status_code == 200

    body = resp.json()
    for key in ("tenant_id", "principal", "tag_convention", "banks_processed", "memories_deleted", "details"):
        assert key in body, f"response missing {key}"
    assert isinstance(body["details"], list)
    assert isinstance(body["banks_processed"], int)
    assert isinstance(body["memories_deleted"], int)


def test_tag_convention_uses_principal_prefix() -> None:
    """The tag the endpoint targets is exactly principal:{principal}.

    Pinning this in a test prevents future drift — callers (Cerebro,
    Synapse) need to tag memories with this exact convention at retain
    time for them to be erasable here.
    """
    client, _ = _build_client()
    with client:
        resp = client.post(
            "/v1/dsar/forget_principal",
            json={"tenant_id": "x", "principal": "user:carol"},
        )
    assert resp.status_code == 200
    assert resp.json()["tag_convention"] == "principal:user:carol"
