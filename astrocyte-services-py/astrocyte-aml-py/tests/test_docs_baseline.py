"""The documents-only baseline behaves like Operator Memory on the AML contract.

A scripted model stands in for the LLM, so these pin the baseline's mechanics:
the catalog is always returned, documents are opened before being rewritten,
updates land at the source, and brains are isolated per bank.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass

import pytest
from fastapi.testclient import TestClient

from astrocyte_aml.docs_baseline import DocsOnlyMemory, create_docs_app


@dataclass
class _Completion:
    text: str


class _ScriptedModel:
    """Answers plan, write, and select prompts from queues; records prompts."""

    def __init__(self, replies: list[dict | str]):
        self.replies = list(replies)
        self.prompts: list[str] = []

    async def complete(self, messages, **kwargs):
        self.prompts.append(messages[-1].content)
        reply = self.replies.pop(0)
        return _Completion(reply if isinstance(reply, str) else json.dumps(reply))


def _doc(slug, content, title=None):
    return {
        "slug": slug,
        "title": title or slug,
        "covers": f"{slug} facts.",
        "open_if": f"asked about {slug}",
        "content": content,
    }


@pytest.mark.asyncio
async def test_first_add_creates_documents_and_catalog_entries(tmp_path):
    model = _ScriptedModel(
        [
            {"open": [], "create": ["Pets"]},
            {"documents": [_doc("pets", "- Has a beagle named Rex (2024-01-01).")]},
        ]
    )
    mem = DocsOnlyMemory(model, tmp_path)
    assert await mem.add("u1", "**user**: I adopted a beagle named Rex.") == 1
    brain = mem.brain("u1")
    assert brain.read("pets") == "- Has a beagle named Rex (2024-01-01)."
    assert [e.slug for e in brain.catalog()] == ["pets"]
    assert "(empty: no documents yet)" in model.prompts[0], (
        "the agent saw the (empty) catalog first"
    )


@pytest.mark.asyncio
async def test_an_update_opens_the_existing_document_and_rewrites_it_at_the_source(
    tmp_path,
):
    model = _ScriptedModel(
        [
            {"open": [], "create": ["Pets"]},
            {"documents": [_doc("pets", "- Has a beagle named Rex.")]},
            {"open": ["pets"], "create": []},
            {"documents": [_doc("pets", "- Rex (beagle) was rehomed in March 2024.")]},
        ]
    )
    mem = DocsOnlyMemory(model, tmp_path)
    await mem.add("u1", "**user**: I adopted a beagle named Rex.")
    await mem.add("u1", "**user**: We rehomed Rex in March.")
    assert "- Has a beagle named Rex." in model.prompts[3], (
        "the old content was read before rewriting"
    )
    assert mem.brain("u1").read("pets") == "- Rex (beagle) was rehomed in March 2024."
    assert len(mem.brain("u1").catalog()) == 1, (
        "updated in place, not a second document"
    )


@pytest.mark.asyncio
async def test_nothing_worth_recording_writes_nothing_and_costs_one_call(tmp_path):
    model = _ScriptedModel([{"open": [], "create": []}])
    mem = DocsOnlyMemory(model, tmp_path)
    assert await mem.add("u1", "**user**: thanks!") == 0
    assert len(model.prompts) == 1


@pytest.mark.asyncio
async def test_unknown_slugs_from_the_model_are_ignored(tmp_path):
    model = _ScriptedModel([{"open": ["does-not-exist"], "create": []}])
    assert await DocsOnlyMemory(model, tmp_path).add("u1", "x") == 0


@pytest.mark.asyncio
async def test_search_returns_the_catalog_then_the_documents_the_agent_opens(tmp_path):
    model = _ScriptedModel(
        [
            {"open": [], "create": ["Pets", "Work"]},
            {
                "documents": [
                    _doc("pets", "Rex the beagle.", "Pets"),
                    _doc("work", "Works at a bakery.", "Work"),
                ]
            },
            {"open": ["work"]},
        ]
    )
    mem = DocsOnlyMemory(model, tmp_path)
    await mem.add("u1", "conversation")
    items = await mem.search("u1", "Where does the user work?", 50)
    assert [i.id for i in items] == ["catalog", "work"]
    assert "**Pets**" in items[0].content and "**Work**" in items[0].content
    assert "Works at a bakery." in items[1].content
    assert "Do not answer the question." in model.prompts[-1]


@pytest.mark.asyncio
async def test_search_on_an_empty_brain_returns_only_the_catalog_without_a_model_call(
    tmp_path,
):
    model = _ScriptedModel([])
    items = await DocsOnlyMemory(model, tmp_path).search("u1", "anything", 50)
    assert [i.id for i in items] == ["catalog"]


@pytest.mark.asyncio
async def test_brains_are_isolated_per_bank(tmp_path):
    model = _ScriptedModel(
        [
            {"open": [], "create": ["Pets"]},
            {"documents": [_doc("pets", "u1's dog.")]},
        ]
    )
    mem = DocsOnlyMemory(model, tmp_path)
    await mem.add("u1", "x")
    assert mem.brain("u2").catalog() == []
    assert [i.id for i in await mem.search("u2", "dog?", 50)] == ["catalog"]


@pytest.mark.asyncio
async def test_concurrent_adds_to_one_bank_are_serialised(tmp_path):
    order: list[str] = []

    class _Slow(_ScriptedModel):
        async def complete(self, messages, **kwargs):
            order.append("start")
            await asyncio.sleep(0.02)
            order.append("end")
            return await super().complete(messages, **kwargs)

    model = _Slow([{"open": [], "create": []}, {"open": [], "create": []}])
    mem = DocsOnlyMemory(model, tmp_path)
    await asyncio.gather(mem.add("u1", "a"), mem.add("u1", "b"))
    assert order == ["start", "end", "start", "end"]


class TestContract:
    def _client(self, model, tmp_path):
        return TestClient(
            create_docs_app(DocsOnlyMemory(model, tmp_path)),
            raise_server_exceptions=False,
        )

    def test_add_and_search_follow_the_aml_shapes(self, tmp_path):
        model = _ScriptedModel(
            [
                {"open": [], "create": ["Pets"]},
                {"documents": [_doc("pets", "Rex.")]},
                {"open": ["pets"]},
            ]
        )
        c = self._client(model, tmp_path)
        body = {
            "request_id": "r1",
            "user_id": "u1",
            "session_id": "s1",
            "messages": [
                {
                    "role": "user",
                    "content": "I adopted Rex.",
                    "timestamp": 1704067200000,
                }
            ],
        }
        r = c.post("/add", json=body)
        assert r.status_code == 200 and r.json()["request_id"] == "r1"
        assert "2024-01-01" in model.prompts[0], "turn timestamps reach the agent"
        s = c.post("/search", json={"query": "pet?", "user_id": "u1", "top_k": 10})
        assert [d["id"] for d in s.json()["data"]] == ["catalog", "pets"]

    def test_unparseable_model_output_is_a_retryable_500(self, tmp_path):
        c = self._client(_ScriptedModel(["not json at all"]), tmp_path)
        body = {
            "request_id": "r1",
            "user_id": "u1",
            "session_id": "s1",
            "messages": [{"role": "user", "content": "x"}],
        }
        assert c.post("/add", json=body).status_code == 500

    def test_empty_messages_is_a_400(self, tmp_path):
        c = self._client(_ScriptedModel([]), tmp_path)
        body = {"request_id": "r1", "user_id": "u1", "session_id": "s1", "messages": []}
        assert c.post("/add", json=body).status_code == 400
