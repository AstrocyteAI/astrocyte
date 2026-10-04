"""``GET /v1/banks/{bank_id}/changes`` — the team-memory change feed (team-memory.md §8, G3).

Rows are seeded straight into the in-memory store with explicit timestamps:
Windows clocks tick every ~15 ms, so consecutive retains can share one.
"""

from __future__ import annotations

import asyncio
import base64
import json
import textwrap
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from astrocyte.types import VectorItem
from fastapi.testclient import TestClient

from astrocyte_gateway import tokens as tk

T0 = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)
BANK = "project:api-1a2b3c"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("ASTROCYTE_CONFIG_PATH", "ASTROCYTE_TOKENS_FILE", "ASTROCYTE_RATE_LIMIT_PER_SECOND"):
        monkeypatch.delenv(name, raising=False)
    # Serve every change at once; TestSettleWindow covers the default hold-back.
    monkeypatch.setenv("ASTROCYTE_CHANGES_SETTLE_SECONDS", "0")


def _item(mid: str, at: datetime = T0, bank: str = BANK) -> VectorItem:
    return VectorItem(
        id=mid,
        bank_id=bank,
        vector=[0.1] * 128,
        text=f"decision {mid}",
        metadata={"_created_at": at.isoformat(), "_actor": "user:alice", "source": "codex"},
        tags=["decision"],
        fact_type="world",
        occurred_at=at - timedelta(hours=1),
        retained_at=at,
    )


def _seed(store, items: list[VectorItem]) -> None:
    # The in-memory store holds no loop-bound state, so a private loop is fine.
    asyncio.run(store.store_vectors(items))


def _dev_app(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ASTROCYTE_AUTH_MODE", "dev")
    from astrocyte_gateway.app import create_app
    from astrocyte_gateway.brain import build_astrocyte

    brain = build_astrocyte()
    return TestClient(create_app(brain)), brain._pipeline.vector_store


def _token_app(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Token mode, access control on (deny by default); returns (client, store, registry)."""
    cfg = tmp_path / "astrocyte.yaml"
    cfg.write_text(
        textwrap.dedent(
            """
            provider_tier: storage
            vector_store: in_memory
            llm_provider: mock
            barriers:
              pii:
                mode: disabled
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


def _changes(client: TestClient, bank: str = BANK, **params):
    return client.get(f"/v1/banks/{bank}/changes", params=params)


def _drain(client: TestClient, limit: int) -> list[dict]:
    seen, cursor = [], None
    while True:
        params = {"limit": limit} | ({"cursor": cursor} if cursor else {})
        body = _changes(client, **params).json()
        seen += body["changes"]
        if not body["has_more"]:
            return seen
        cursor = body["next_cursor"]


class TestChangeFeed:
    def test_live_row_shape(self, monkeypatch: pytest.MonkeyPatch):
        client, store = _dev_app(monkeypatch)
        _seed(store, [_item("a1b2c3d4e5f60718")])
        res = _changes(client)
        assert res.status_code == 200
        body = res.json()
        assert body["has_more"] is False
        assert body["changes"] == [
            {
                "id": "a1b2c3d4e5f60718",
                "deleted": False,
                "changed_at": T0.isoformat(),
                "text": "decision a1b2c3d4e5f60718",
                "occurred_at": (T0 - timedelta(hours=1)).isoformat(),
                "retained_at": T0.isoformat(),
                "tags": ["decision"],
                "fact_type": "world",
                "memory_layer": None,
                "metadata": {"_created_at": T0.isoformat(), "_actor": "user:alice", "source": "codex"},
            }
        ]
        cursor = body["next_cursor"]
        payload = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
        assert payload == {"changed_at": T0.isoformat(), "id": "a1b2c3d4e5f60718"}

    def test_ordered_and_resumes_without_gaps_or_repeats(self, monkeypatch: pytest.MonkeyPatch):
        client, store = _dev_app(monkeypatch)
        # 30 rows share one changed_at (only the id orders them), plus one each side.
        items = [_item(f"m{i:02d}") for i in range(30)]
        items += [_item("first", T0 - timedelta(seconds=1)), _item("zlast", T0 + timedelta(seconds=1))]
        _seed(store, items)
        _seed(store, [_item("other-bank", bank="project:web-9f9f9f")])
        expected = ["first", *(f"m{i:02d}" for i in range(30)), "zlast"]
        for limit in (1, 7, 30, 31, 32, 1000):
            assert [c["id"] for c in _drain(client, limit)] == expected, limit

    def test_limit_next_cursor_and_has_more(self, monkeypatch: pytest.MonkeyPatch):
        client, store = _dev_app(monkeypatch)
        empty = _changes(client).json()
        assert empty == {"changes": [], "next_cursor": None, "has_more": False}
        _seed(store, [_item(m) for m in ("a", "b", "c")])
        page = _changes(client, limit=2).json()
        assert [c["id"] for c in page["changes"]] == ["a", "b"] and page["has_more"] is True
        rest = _changes(client, limit=2, cursor=page["next_cursor"]).json()
        assert [c["id"] for c in rest["changes"]] == ["c"] and rest["has_more"] is False
        idle = _changes(client, cursor=rest["next_cursor"]).json()
        assert idle == {"changes": [], "next_cursor": rest["next_cursor"], "has_more": False}

    def test_limit_is_clamped(self, monkeypatch: pytest.MonkeyPatch):
        client, store = _dev_app(monkeypatch)
        _seed(store, [_item(f"m{i:04d}") for i in range(1003)])
        assert len(_changes(client, limit=0).json()["changes"]) == 1
        big = _changes(client, limit=5000).json()
        assert len(big["changes"]) == 1000 and big["has_more"] is True
        assert len(_changes(client).json()["changes"]) == 100

    def test_tombstone_after_forget(self, monkeypatch: pytest.MonkeyPatch):
        client, store = _dev_app(monkeypatch)
        _seed(store, [_item("keep"), _item("gone")])
        cursor = _changes(client).json()["next_cursor"]
        res = client.post("/v1/forget", json={"bank_id": BANK, "memory_ids": ["gone"]})
        assert res.status_code == 200, res.text
        after = _changes(client, cursor=cursor).json()
        [tomb] = after["changes"]
        assert set(tomb) == {"id", "deleted", "changed_at"}
        assert tomb["id"] == "gone" and tomb["deleted"] is True
        assert datetime.fromisoformat(tomb["changed_at"]) > T0
        assert [(c["id"], c["deleted"]) for c in _changes(client).json()["changes"]] == [
            ("keep", False),
            ("gone", True),
        ]

    @pytest.mark.parametrize(
        "cursor",
        [
            "garbage",
            base64.urlsafe_b64encode(b'{"id": "a"}').decode(),
            base64.urlsafe_b64encode(b'{"changed_at": "not a date", "id": "a"}').decode(),
        ],
    )
    def test_invalid_cursor_is_400(self, monkeypatch: pytest.MonkeyPatch, cursor: str):
        client, _ = _dev_app(monkeypatch)
        res = _changes(client, cursor=cursor)
        assert res.status_code == 400
        assert res.json() == {"detail": "invalid cursor"}

    def test_invalid_bank_id_is_400(self, monkeypatch: pytest.MonkeyPatch):
        client, _ = _dev_app(monkeypatch)
        assert _changes(client, bank="bad bank!").status_code == 400

    def test_store_without_change_feed_is_501(self, monkeypatch: pytest.MonkeyPatch):
        client, store = _dev_app(monkeypatch)
        monkeypatch.setattr(store, "list_changes", None)
        res = _changes(client)
        assert res.status_code == 501
        assert res.json()["capability"] == "list_changes"


class TestSettleWindow:
    def test_fresh_changes_wait_for_the_default_window(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.delenv("ASTROCYTE_CHANGES_SETTLE_SECONDS")
        client, store = _dev_app(monkeypatch)
        _seed(store, [_item("old"), _item("fresh", datetime.now(UTC))])
        page = _changes(client).json()
        assert [c["id"] for c in page["changes"]] == ["old"] and page["has_more"] is False
        # A forget is fresh too: its tombstone waits like any other change.
        assert client.post("/v1/forget", json={"bank_id": BANK, "memory_ids": ["old"]}).status_code == 200
        assert _changes(client, cursor=page["next_cursor"]).json()["changes"] == []
        monkeypatch.setenv("ASTROCYTE_CHANGES_SETTLE_SECONDS", "0")
        later = _changes(client, cursor=page["next_cursor"]).json()["changes"]
        assert [(c["id"], c["deleted"]) for c in later] == [("fresh", False), ("old", True)]

    @pytest.mark.parametrize("raw", ["nonsense", "-3"])
    def test_bad_values(self, monkeypatch: pytest.MonkeyPatch, raw: str):
        from astrocyte_gateway.app import _changes_settle_seconds

        monkeypatch.setenv("ASTROCYTE_CHANGES_SETTLE_SECONDS", raw)
        assert _changes_settle_seconds() == (5.0 if raw == "nonsense" else 0.0)


class TestChangeFeedPermissions:
    def test_read_permission_is_required(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
        client, store, registry = _token_app(monkeypatch, tmp_path)
        _seed(store, [_item("a")])
        reader, _ = tk.create_token(registry, principal="user:alice", banks=["project:*"], permissions=["read"])
        writer, _ = tk.create_token(registry, principal="user:bob", banks=["project:*"], permissions=["write"])
        other, _ = tk.create_token(registry, principal="user:carol", banks=["project:web-*"], permissions=["read"])

        ok = client.get(f"/v1/banks/{BANK}/changes", headers={"Authorization": f"Bearer {reader}"})
        assert ok.status_code == 200 and [c["id"] for c in ok.json()["changes"]] == ["a"]
        for token in (writer, other):
            res = client.get(f"/v1/banks/{BANK}/changes", headers={"Authorization": f"Bearer {token}"})
            assert res.status_code == 403, res.text
        # Denied before the cursor is even looked at.
        res = client.get(
            f"/v1/banks/{BANK}/changes", params={"cursor": "garbage"}, headers={"Authorization": f"Bearer {writer}"}
        )
        assert res.status_code == 403
        assert client.get(f"/v1/banks/{BANK}/changes").status_code == 401
