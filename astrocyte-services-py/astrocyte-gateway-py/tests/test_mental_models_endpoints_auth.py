"""Tests that mental-models, observations/invalidate, and debug/recall
endpoints route through ``brain._policy.check_access`` for symmetry with
``/v1/recall``, ``/v1/retain``, ``/v1/reflect``, ``/v1/forget``.

The ``ctx`` threading was added so these endpoints aren't a silent
bypass when operators enable ``access_control.enabled = True``. Today's
default behaviour (access_control disabled) is preserved — these tests
verify both modes.
"""

from __future__ import annotations

import textwrap

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(autouse=True)
def _dev_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ASTROCYTE_AUTH_MODE", "dev")
    monkeypatch.delenv("ASTROCYTE_RATE_LIMIT_PER_SECOND", raising=False)


def _build_app(tmp_path, *, access_control_enabled: bool, default_policy: str = "open"):
    """Build a TestClient-ready app with an in-memory brain.

    Uses ``in_memory`` providers so no Postgres is required. The mental-models
    endpoints raise 501 unless a wiki_store is wired — ``in_memory`` provides
    one, so the access-check path executes BEFORE the 501 short-circuit.
    """
    cfg = tmp_path / "astrocyte.yaml"
    cfg.write_text(
        textwrap.dedent(
            f"""
            provider_tier: storage
            vector_store: in_memory
            graph_store: in_memory
            wiki_store: in_memory
            llm_provider: mock
            access_control:
              enabled: {str(access_control_enabled).lower()}
              default_policy: {default_policy}
            """
        ),
        encoding="utf-8",
    )
    import os
    os.environ["ASTROCYTE_CONFIG_PATH"] = str(cfg)

    from astrocyte_gateway.app import create_app

    return TestClient(create_app())


# ---------------------------------------------------------------------------
# Backward compatibility: when access_control is OFF, anonymous requests
# pass through exactly as before. The added ``ctx`` parameter is a no-op
# at the policy layer.
# ---------------------------------------------------------------------------


class TestBackwardCompatNoAccessControl:
    """``access_control.enabled = false`` — same effective behaviour as before
    the ctx-threading change. Anonymous requests succeed."""

    def test_create_mental_model_anonymous_succeeds(self, tmp_path):
        client = _build_app(tmp_path, access_control_enabled=False)
        resp = client.post(
            "/v1/mental-models",
            json={
                "bank_id": "bank-1",
                "model_id": "model:alice",
                "title": "Alice",
                "content": "Alice prefers async updates.",
            },
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["model_id"] == "model:alice"

    def test_list_mental_models_anonymous_succeeds(self, tmp_path):
        client = _build_app(tmp_path, access_control_enabled=False)
        resp = client.get("/v1/mental-models", params={"bank_id": "bank-1"})
        assert resp.status_code == 200
        # Empty list is fine — the access check is what we're verifying.
        assert "models" in resp.json()

    def test_get_mental_model_anonymous_succeeds(self, tmp_path):
        client = _build_app(tmp_path, access_control_enabled=False)
        client.post(
            "/v1/mental-models",
            json={
                "bank_id": "bank-1",
                "model_id": "model:alice",
                "title": "Alice",
                "content": "x",
            },
        )
        resp = client.get(
            "/v1/mental-models/model:alice",
            params={"bank_id": "bank-1"},
        )
        assert resp.status_code == 200
        assert resp.json()["model_id"] == "model:alice"

    def test_refresh_mental_model_anonymous_succeeds(self, tmp_path):
        client = _build_app(tmp_path, access_control_enabled=False)
        client.post(
            "/v1/mental-models",
            json={
                "bank_id": "bank-1",
                "model_id": "model:alice",
                "title": "Alice",
                "content": "v1 content",
            },
        )
        resp = client.post(
            "/v1/mental-models/model:alice/refresh",
            json={"bank_id": "bank-1", "content": "v2 content"},
        )
        assert resp.status_code == 200
        assert resp.json()["content"] == "v2 content"

    def test_delete_mental_model_anonymous_succeeds(self, tmp_path):
        client = _build_app(tmp_path, access_control_enabled=False)
        client.post(
            "/v1/mental-models",
            json={
                "bank_id": "bank-1",
                "model_id": "model:bob",
                "title": "Bob",
                "content": "x",
            },
        )
        resp = client.delete(
            "/v1/mental-models/model:bob",
            params={"bank_id": "bank-1"},
        )
        assert resp.status_code == 200
        assert resp.json() == {"deleted": True}

    def test_invalidate_observations_anonymous_succeeds_or_501(self, tmp_path):
        """When access_control is disabled, the only failure mode here is
        the 501 'observation consolidator not configured' (because in_memory
        providers don't wire one up). The ACCESS check must NOT 403."""
        client = _build_app(tmp_path, access_control_enabled=False)
        resp = client.post(
            "/v1/observations/invalidate",
            json={"bank_id": "bank-1", "source_ids": ["m1"]},
        )
        # Either 200 (consolidator configured) or 501 (not). Never 403.
        assert resp.status_code in (200, 501), resp.text


# ---------------------------------------------------------------------------
# Access-control enforcement: with access_control.enabled = True and
# default_policy = closed, the endpoints DENY requests without a matching
# grant. This is the gap the ctx-threading change closes.
# ---------------------------------------------------------------------------


class TestAccessControlEnforced:
    """``access_control.enabled = true, default_policy = closed`` — anonymous
    requests are 403. Mental-models endpoints now enforce same as recall."""

    def test_create_anonymous_is_denied(self, tmp_path):
        client = _build_app(tmp_path, access_control_enabled=True, default_policy="closed")
        resp = client.post(
            "/v1/mental-models",
            json={
                "bank_id": "bank-1",
                "model_id": "m1",
                "title": "T",
                "content": "C",
            },
        )
        assert resp.status_code == 403, resp.text

    def test_list_anonymous_is_denied(self, tmp_path):
        client = _build_app(tmp_path, access_control_enabled=True, default_policy="closed")
        resp = client.get("/v1/mental-models", params={"bank_id": "bank-1"})
        assert resp.status_code == 403

    def test_get_anonymous_is_denied(self, tmp_path):
        client = _build_app(tmp_path, access_control_enabled=True, default_policy="closed")
        resp = client.get("/v1/mental-models/anything", params={"bank_id": "bank-1"})
        assert resp.status_code == 403

    def test_refresh_anonymous_is_denied(self, tmp_path):
        client = _build_app(tmp_path, access_control_enabled=True, default_policy="closed")
        resp = client.post(
            "/v1/mental-models/anything/refresh",
            json={"bank_id": "bank-1", "content": "x"},
        )
        assert resp.status_code == 403

    def test_delete_anonymous_is_denied(self, tmp_path):
        client = _build_app(tmp_path, access_control_enabled=True, default_policy="closed")
        resp = client.delete("/v1/mental-models/anything", params={"bank_id": "bank-1"})
        assert resp.status_code == 403

    def test_invalidate_observations_anonymous_is_denied(self, tmp_path):
        client = _build_app(tmp_path, access_control_enabled=True, default_policy="closed")
        resp = client.post(
            "/v1/observations/invalidate",
            json={"bank_id": "bank-1", "source_ids": ["m1"]},
        )
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Symmetry: the existing /v1/recall endpoint should behave the same way
# under the same config — proves we're not introducing a different model.
# ---------------------------------------------------------------------------


class TestSymmetryWithRecall:
    """Whatever access_control does for /v1/recall, it now does for
    mental-models endpoints too. This is the WHOLE point of the change."""

    def test_recall_and_mental_models_both_403_under_closed_policy(self, tmp_path):
        client = _build_app(tmp_path, access_control_enabled=True, default_policy="closed")

        recall_resp = client.post(
            "/v1/recall",
            json={"query": "anything", "bank_id": "bank-1"},
        )
        mm_resp = client.post(
            "/v1/mental-models",
            json={
                "bank_id": "bank-1",
                "model_id": "m1",
                "title": "T",
                "content": "C",
            },
        )

        # Both must produce 403 — proving mental-models now mirrors recall.
        assert recall_resp.status_code == 403
        assert mm_resp.status_code == 403
