"""Erasure on the gateway (team-memory.md §8, G4): ``/v1/forget`` with
``erase`` and the DSAR sweep by ``_actor``. Token mode, access control on."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from astrocyte_gateway import tokens as tk


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("ASTROCYTE_CONFIG_PATH", "ASTROCYTE_TOKENS_FILE", "ASTROCYTE_RATE_LIMIT_PER_SECOND"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ASTROCYTE_CHANGES_SETTLE_SECONDS", "0")


@pytest.fixture
def gw(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    cfg = tmp_path / "astrocyte.yaml"
    cfg.write_text(textwrap.dedent("""
        provider_tier: storage
        vector_store: in_memory
        llm_provider: mock
        barriers:
          pii:
            mode: disabled
        access_control:
          enabled: true
          default_policy: deny
        """), encoding="utf-8")
    registry = tmp_path / "tokens.yaml"
    registry.write_text("tokens: []\n", encoding="utf-8")
    monkeypatch.setenv("ASTROCYTE_AUTH_MODE", "token")
    monkeypatch.setenv("ASTROCYTE_TOKENS_FILE", str(registry))
    monkeypatch.setenv("ASTROCYTE_CONFIG_PATH", str(cfg))
    from astrocyte_gateway.app import create_app
    from astrocyte_gateway.brain import build_astrocyte

    brain = build_astrocyte()
    store = brain._pipeline.vector_store

    async def list_banks():  # as the SQL stores have it (their suites test it): finds project:* banks
        banks: dict[str, int] = {}
        for item in store._vectors.values():
            banks[item.bank_id] = banks.get(item.bank_id, 0) + 1
        return [(b, n, None) for b, n in sorted(banks.items())]

    store.list_banks = list_banks
    client = TestClient(create_app(brain))

    def auth(principal: str, permissions=("read", "write", "forget")) -> dict:
        token, _ = tk.create_token(registry, principal=principal, banks=["project:*"], permissions=list(permissions))
        return {"Authorization": f"Bearer {token}"}

    client.login = auth  # type: ignore[attr-defined]
    return client


def _push(gw, headers, bank, *records):
    reply = gw.post(f"/v1/banks/{bank}/sync/push", json={"records": [{"id": i, "text": t} for i, t in records]},
                    headers=headers)
    assert reply.status_code == 200, reply.text
    return reply.json()["results"]


def _feed(gw, headers, bank) -> list[tuple[str, bool]]:
    return [(c["id"], c["deleted"]) for c in gw.get(f"/v1/banks/{bank}/changes", headers=headers).json()["changes"]]


class TestForgetWithErase:
    def test_erase_keeps_the_tombstone_and_the_id_forgotten(self, gw):
        alice = gw.login("user:alice")
        _push(gw, alice, "project:a", ("aaaaaaaa1", "The old key rotation runbook."))
        reply = gw.post("/v1/forget", json={"bank_id": "project:a", "memory_ids": ["aaaaaaaa1"], "erase": True},
                        headers=alice)
        assert reply.status_code == 200, reply.text
        assert reply.json()["deleted_count"] == 1 and reply.json()["erased_count"] == 1
        assert _feed(gw, alice, "project:a") == [("aaaaaaaa1", True)], "mirrors still learn of it"
        [again] = _push(gw, alice, "project:a", ("aaaaaaaa1", "The old key rotation runbook."))
        assert again["status"] == "rejected"

    def test_erase_needs_ids(self, gw):
        reply = gw.post("/v1/forget", json={"bank_id": "project:a", "scope": "all", "erase": True},
                        headers=gw.login("user:alice"))
        assert reply.status_code == 400 and "memory_ids" in reply.json()["detail"]

    def test_erase_needs_forget_permission(self, gw):
        reader = gw.login("user:reader", permissions=("read", "write"))
        _push(gw, reader, "project:a", ("aaaaaaaa1", "A note."))
        reply = gw.post("/v1/forget", json={"bank_id": "project:a", "memory_ids": ["aaaaaaaa1"], "erase": True},
                        headers=reader)
        assert reply.status_code == 403

    def test_without_erase_the_answer_is_unchanged(self, gw):
        alice = gw.login("user:alice")
        _push(gw, alice, "project:a", ("aaaaaaaa1", "A note."))
        reply = gw.post("/v1/forget", json={"bank_id": "project:a", "memory_ids": ["aaaaaaaa1"]}, headers=alice)
        assert "erased_count" not in reply.json() or reply.json()["erased_count"] is None


class TestDsarByActor:
    def test_erases_what_the_principal_saved_across_the_tenants_banks(self, gw):
        alice, bob = gw.login("user:alice"), gw.login("user:bob")
        _push(gw, alice, "project:a", ("alice-a-1", "Alice: deploys go out on Tuesdays."))
        _push(gw, alice, "project:b", ("alice-b-1", "Alice: flags live in LaunchDarkly."))
        _push(gw, bob, "project:a", ("bob-a-001", "Bob: staging is at s.test."))
        dpo = gw.login("user:dpo", permissions=("read", "write", "forget", "admin"))
        reply = gw.post("/v1/dsar/forget_principal", json={"tenant_id": "project", "principal": "user:alice"},
                        headers=dpo)
        assert reply.status_code == 200, reply.text
        body = reply.json()
        assert body["memories_deleted"] == 2 and body["memories_erased"] == 2
        assert {d["bank_id"]: (d["deleted"], d["erased"]) for d in body["details"]} == {
            "project:a": (1, 1), "project:b": (1, 1)}
        assert _feed(gw, bob, "project:a") == [("bob-a-001", False), ("alice-a-1", True)]
