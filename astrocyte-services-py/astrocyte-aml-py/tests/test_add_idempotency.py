"""Add must be idempotent under the platform's retry policy.

AML retries 408/429/500/524 up to 32 times. A retry of an Add that is still
running, or that already succeeded, must not ingest the batch a second time:
duplicate memories crowd the top-k, and the run-to-run drift they cause is a
direct risk under the reproduction clause. A failed Add, by contrast, must
genuinely re-run on retry.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest
from test_aml_contract import ADD_BODY, _RetainResult

from astrocyte_aml import app as app_module
from astrocyte_aml.app import create_app


class _GatedBrain:
    """Fake brain whose retain blocks until released, so retries overlap it."""

    def __init__(self) -> None:
        self.retain_calls: list[dict[str, Any]] = []
        self.gate = asyncio.Event()
        self.entered = asyncio.Event()
        self.fail_next: Exception | None = None

    async def retain(self, content: str, **kwargs: Any) -> _RetainResult:
        self.retain_calls.append({"content": content, **kwargs})
        self.entered.set()
        await self.gate.wait()
        if self.fail_next is not None:
            exc, self.fail_next = self.fail_next, None
            raise exc
        return _RetainResult()


def _client(app: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")


@pytest.mark.asyncio
async def test_retry_after_success_replays_without_reingesting():
    brain = _GatedBrain()
    brain.gate.set()
    async with _client(create_app(brain=brain)) as c:
        first = await c.post("/add", json=ADD_BODY)
        second = await c.post("/add", json=ADD_BODY)
    assert first.status_code == second.status_code == 200
    assert second.json() == first.json()
    assert len(brain.retain_calls) == 1


@pytest.mark.asyncio
async def test_retry_during_inflight_add_joins_it():
    brain = _GatedBrain()
    async with _client(create_app(brain=brain)) as c:
        first = asyncio.create_task(c.post("/add", json=ADD_BODY))
        await brain.entered.wait()
        retry = asyncio.create_task(c.post("/add", json=ADD_BODY))
        await asyncio.sleep(0.05)
        assert not retry.done(), "retry must wait for the in-flight retain"
        brain.gate.set()
        r1, r2 = await first, await retry
    assert r1.status_code == r2.status_code == 200
    assert len(brain.retain_calls) == 1


@pytest.mark.asyncio
async def test_cancelled_caller_does_not_cancel_the_retain_a_retry_waits_on():
    """The disconnect that triggers a retry must not abort the shared work."""
    brain = _GatedBrain()
    async with _client(create_app(brain=brain)) as c:
        first = asyncio.create_task(c.post("/add", json=ADD_BODY))
        await brain.entered.wait()
        first.cancel()
        retry = asyncio.create_task(c.post("/add", json=ADD_BODY))
        await asyncio.sleep(0.05)
        brain.gate.set()
        r2 = await retry
    assert r2.status_code == 200
    assert len(brain.retain_calls) == 1


@pytest.mark.asyncio
async def test_failure_is_not_cached_so_retry_reruns():
    brain = _GatedBrain()
    brain.gate.set()
    brain.fail_next = RuntimeError("transient provider error")
    async with _client(create_app(brain=brain)) as c:
        failed = await c.post("/add", json=ADD_BODY)
        retried = await c.post("/add", json=ADD_BODY)
    assert failed.status_code == 500
    assert retried.status_code == 200
    assert len(brain.retain_calls) == 2


@pytest.mark.asyncio
async def test_inflight_failure_fails_every_joined_caller():
    brain = _GatedBrain()
    brain.fail_next = RuntimeError("boom")
    async with _client(create_app(brain=brain)) as c:
        first = asyncio.create_task(c.post("/add", json=ADD_BODY))
        await brain.entered.wait()
        joined = asyncio.create_task(c.post("/add", json=ADD_BODY))
        await asyncio.sleep(0.05)
        brain.gate.set()
        r1, r2 = await first, await joined
    assert r1.status_code == r2.status_code == 500
    assert len(brain.retain_calls) == 1


@pytest.mark.asyncio
async def test_reused_request_id_with_different_content_is_still_stored():
    """Coalescing on request_id alone would silently drop a distinct batch."""
    brain = _GatedBrain()
    brain.gate.set()
    other = {**ADD_BODY, "messages": [{"role": "user", "content": "A different turn."}]}
    async with _client(create_app(brain=brain)) as c:
        assert (await c.post("/add", json=ADD_BODY)).status_code == 200
        assert (await c.post("/add", json=other)).status_code == 200
    assert len(brain.retain_calls) == 2


@pytest.mark.asyncio
async def test_same_request_id_under_another_user_is_isolated():
    brain = _GatedBrain()
    brain.gate.set()
    other_user = {**ADD_BODY, "user_id": "eval:run_abc123:locomo:conv-1"}
    async with _client(create_app(brain=brain)) as c:
        await c.post("/add", json=ADD_BODY)
        await c.post("/add", json=other_user)
    assert [call["bank_id"] for call in brain.retain_calls] == [
        ADD_BODY["user_id"],
        other_user["user_id"],
    ]


@pytest.mark.asyncio
async def test_replay_memory_is_bounded(monkeypatch):
    monkeypatch.setattr(app_module, "ADD_REPLAY_CAPACITY", 2)
    brain = _GatedBrain()
    brain.gate.set()
    app = create_app(brain=brain)
    async with _client(app) as c:
        for i in range(3):
            await c.post("/add", json={**ADD_BODY, "request_id": f"r{i}"})
        # r0 was evicted, so its retry re-runs (dedup in the pipeline still
        # applies); r2 is remembered and replays.
        await c.post("/add", json={**ADD_BODY, "request_id": "r0"})
        await c.post("/add", json={**ADD_BODY, "request_id": "r2"})
    assert len(app.state.add_done) == 2
    assert len(brain.retain_calls) == 4
