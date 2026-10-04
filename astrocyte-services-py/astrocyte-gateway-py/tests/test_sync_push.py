"""``POST /v1/banks/{bank_id}/sync/push`` — team-memory batch push (team-memory.md §8, G2)."""

from __future__ import annotations

import asyncio
import textwrap
from pathlib import Path

import pytest
from astrocyte.types import VectorItem
from fastapi.testclient import TestClient

from astrocyte_gateway import tokens as tk

BANK = "project:api-1a2b3c"
OTHER = "project:web-9f9f9f"
ID1, ID2 = "9f2c41d07ab3e815", "1c0d5e7f9a2b4c6d"
TEXT = "We moved the job queue from SQS to Kafka."


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("ASTROCYTE_CONFIG_PATH", "ASTROCYTE_TOKENS_FILE", "ASTROCYTE_RATE_LIMIT_PER_SECOND"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ASTROCYTE_CHANGES_SETTLE_SECONDS", "0")


def _app(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, pii: str = "disabled", action: str = "redact"):
    """Token mode, access control on; returns (client, store, registry)."""
    cfg = tmp_path / "astrocyte.yaml"
    cfg.write_text(
        textwrap.dedent(
            f"""
            provider_tier: storage
            vector_store: in_memory
            llm_provider: mock
            barriers:
              pii:
                mode: {pii}
                action: {action}
            access_control:
              enabled: true
              default_policy: deny
            """
        ),
        encoding="utf-8",
    )
    registry = tmp_path / "tokens.yaml"
    registry.write_text("tokens: []\n", encoding="utf-8")
    monkeypatch.setenv("ASTROCYTE_AUTH_MODE", "token")
    monkeypatch.setenv("ASTROCYTE_TOKENS_FILE", str(registry))
    monkeypatch.setenv("ASTROCYTE_CONFIG_PATH", str(cfg))
    from astrocyte_gateway.app import create_app
    from astrocyte_gateway.brain import build_astrocyte

    brain = build_astrocyte()
    return TestClient(create_app(brain)), brain._pipeline.vector_store, registry


def _token(registry: Path, principal: str = "user:alice", permissions=("read", "write", "forget")) -> dict:
    token, _ = tk.create_token(registry, principal=principal, banks=["project:*"], permissions=list(permissions))
    return {"Authorization": f"Bearer {token}"}


def _push(client: TestClient, headers: dict, *records: dict, bank: str = BANK):
    return client.post(f"/v1/banks/{bank}/sync/push", json={"records": list(records)}, headers=headers)


def _rec(mid: str = ID1, text: str = TEXT, **kw) -> dict:
    return {"id": mid, "text": text, **kw}


def _rows(store, bank: str = BANK) -> list[VectorItem]:
    return asyncio.run(store.list_vectors(bank))


class TestPush:
    def test_stored_row_has_the_pushed_id_and_is_recalled(self, monkeypatch, tmp_path):
        client, store, registry = _app(monkeypatch, tmp_path)
        alice = _token(registry)
        res = _push(
            client,
            alice,
            _rec(
                occurred_at="2026-10-02T09:14:03+00:00",
                tags=["decision"],
                fact_type="world",
                metadata={"_created_at": "2026-10-02T09:14:03.118Z", "_retain_id": "r1", "_chunk_index": 0,
                          "session_id": "s1", "source": "codex", "_actor": "user:mallory"},
            ),
        )
        assert res.status_code == 200, res.text
        assert res.json() == {"results": [{"id": ID1, "status": "stored"}]}
        [row] = _rows(store)
        assert (row.id, row.text, row.tags, row.fact_type) == (ID1, TEXT, ["decision"], "world")
        assert row.metadata == {
            "_created_at": "2026-10-02T09:14:03.118Z",
            "_retain_id": "r1",
            "_chunk_index": 0,
            "session_id": "s1",
            "source": "codex",
            "_actor": "user:alice",  # the token's principal, not the client's claim
        }
        hits = client.post("/v1/recall", json={"query": "job queue Kafka", "bank_id": BANK}, headers=alice).json()
        assert [h["memory_id"] for h in hits["hits"]] == [ID1]
        feed = client.get(f"/v1/banks/{BANK}/changes", headers=alice).json()
        assert [c["id"] for c in feed["changes"]] == [ID1]

    def test_unchanged_duplicate_and_rejected(self, monkeypatch, tmp_path):
        client, store, registry = _app(monkeypatch, tmp_path)
        alice = _token(registry)
        assert _push(client, alice, _rec(metadata={"source": "codex"})).json()["results"][0]["status"] == "stored"
        res = _push(
            client,
            alice,
            _rec(),  # same id and text, but no metadata now
            _rec(metadata={"source": "codex", "status": "stale"}),  # same text, other metadata
            _rec(text="We use SQS."),  # same id, other text
            _rec(ID2),  # other id, same text
        )
        assert res.json()["results"] == [
            {"id": ID1, "status": "unchanged", "reason": "text unchanged; metadata updates are not accepted by push"},
            {"id": ID1, "status": "unchanged", "reason": "text unchanged; metadata updates are not accepted by push"},
            {"id": ID1, "status": "rejected", "reason": "id is already in use with different text; memory text is immutable"},
            {"id": ID2, "status": "duplicate", "duplicate_of": ID1},
        ]
        [row] = _rows(store)
        assert row.text == TEXT and row.metadata == {"source": "codex", "_actor": "user:alice"}

    def test_exact_repush_is_unchanged_without_a_reason(self, monkeypatch, tmp_path):
        client, _, registry = _app(monkeypatch, tmp_path)
        alice = _token(registry)
        _push(client, alice, _rec(tags=["a"], metadata={"source": "codex"}))
        again = _push(client, alice, _rec(tags=["a"], metadata={"source": "codex"})).json()
        assert again == {"results": [{"id": ID1, "status": "unchanged"}]}

    def test_id_in_another_bank_is_rejected_and_left_alone(self, monkeypatch, tmp_path):
        client, store, registry = _app(monkeypatch, tmp_path)
        secret = "Web team: the staging password rotates on Fridays."
        asyncio.run(store.store_vectors([VectorItem(id=ID1, bank_id=OTHER, vector=[0.5] * 128, text=secret)]))
        res = _push(client, _token(registry), _rec(ID1, "takeover"))
        [result] = res.json()["results"]
        assert result["status"] == "rejected" and secret not in res.text and OTHER not in res.text
        [other] = _rows(store, OTHER)
        assert (other.id, other.bank_id, other.text, other.vector) == (ID1, OTHER, secret, [0.5] * 128)
        assert _rows(store) == []

    def test_forgotten_id_cannot_be_pushed_back(self, monkeypatch, tmp_path):
        client, store, registry = _app(monkeypatch, tmp_path)
        alice = _token(registry)
        _push(client, alice, _rec())
        assert client.post("/v1/forget", json={"bank_id": BANK, "memory_ids": [ID1]}, headers=alice).status_code == 200
        [result] = _push(client, alice, _rec()).json()["results"]
        assert result == {"id": ID1, "status": "rejected", "reason": "id was forgotten; a forgotten memory can't be pushed again"}
        assert _rows(store) == []

    def test_pii_redacted(self, monkeypatch, tmp_path):
        client, store, registry = _app(monkeypatch, tmp_path, pii="regex", action="redact")
        res = _push(client, _token(registry), _rec(text="Ping alice@example.com about the queue."))
        assert res.json()["results"][0]["status"] == "stored"
        assert "alice@example.com" not in _rows(store)[0].text

    def test_pii_rejected(self, monkeypatch, tmp_path):
        client, store, registry = _app(monkeypatch, tmp_path, pii="regex", action="reject")
        res = _push(client, _token(registry), _rec(text="Ping alice@example.com about the queue."), _rec(ID2, "Clean."))
        assert res.status_code == 200
        first, second = res.json()["results"]
        assert first["status"] == "rejected" and "PII detected" in first["reason"]
        assert second["status"] == "stored"
        assert [r.id for r in _rows(store)] == [ID2]


class TestPushAuthAndValidation:
    def test_write_permission_is_required(self, monkeypatch, tmp_path):
        client, store, registry = _app(monkeypatch, tmp_path)
        reader = _token(registry, "user:reader", ("read",))
        assert _push(client, reader, _rec()).status_code == 403
        assert client.post(f"/v1/banks/{BANK}/sync/push", json={"records": [_rec()]}).status_code == 401
        assert _rows(store) == []
        assert _push(client, _token(registry, "user:writer", ("write",)), _rec()).status_code == 200

    @pytest.mark.parametrize(
        ("records", "field"),
        [
            ([_rec(f"id{i:08d}", f"text {i}") for i in range(101)], "records"),
            ([_rec("short")], "id"),
            ([_rec("has spaces!!")], "id"),
            ([_rec("x" * 65)], "id"),
            ([_rec(text="")], "text"),
            ([_rec(content_hash="md5:abc")], "content_hash"),
            ([_rec(metadata={"nested": {"a": 1}})], "metadata"),
        ],
    )
    def test_malformed_body_is_400(self, monkeypatch, tmp_path, records, field):
        client, store, registry = _app(monkeypatch, tmp_path)
        res = _push(client, _token(registry), *records)
        assert res.status_code == 400, res.text
        assert field in res.json()["detail"]
        assert _rows(store) == []

    def test_exactly_100_records_is_accepted(self, monkeypatch, tmp_path):
        client, _, registry = _app(monkeypatch, tmp_path)
        res = _push(client, _token(registry), *[_rec(f"id{i:08d}", f"memory {i} " + "x" * i) for i in range(100)])
        assert res.status_code == 200 and len(res.json()["results"]) == 100

    def test_anonymous_dev_request_drops_a_client_actor(self, monkeypatch):
        monkeypatch.setenv("ASTROCYTE_AUTH_MODE", "dev")
        from astrocyte_gateway.app import create_app
        from astrocyte_gateway.brain import build_astrocyte

        brain = build_astrocyte()
        client = TestClient(create_app(brain))
        res = client.post(
            f"/v1/banks/{BANK}/sync/push", json={"records": [_rec(metadata={"_actor": "user:mallory", "source": "x"})]}
        )
        assert res.status_code == 200
        [row] = _rows(brain._pipeline.vector_store)
        assert row.metadata == {"source": "x"}

    def test_store_without_push_support_is_501(self, monkeypatch, tmp_path):
        client, store, registry = _app(monkeypatch, tmp_path)
        monkeypatch.setattr(store, "insert_vectors", None)
        res = _push(client, _token(registry), _rec())
        assert res.status_code == 501 and res.json()["capability"] == "sync_push"
