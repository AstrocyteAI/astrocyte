"""retain(source=...) reaches recall hits on the default (pipeline) path."""

from __future__ import annotations

from datetime import datetime, timezone

from astrocyte import Astrocyte
from astrocyte.pipeline.provenance import SOURCE_KEY, stored_source


def _brain(tmp_path) -> Astrocyte:
    cfg = tmp_path / "astrocyte.yaml"
    cfg.write_text("vector_store: in_memory\nllm_provider: mock\nbarriers:\n  pii:\n    mode: disabled\n",
                   encoding="utf-8")
    return Astrocyte.from_config(cfg)


async def _hit(brain: Astrocyte, query: str, needle: str):
    result = await brain.recall(query, bank_id="b1", max_results=10)
    return next(h for h in result.hits if needle in h.text)


async def test_source_and_occurred_at_come_back_on_recall(tmp_path):
    brain = _brain(tmp_path)
    when = datetime(2026, 9, 30, 14, tzinfo=timezone.utc)
    await brain.retain("The staging deploy freeze moved to Thursdays.", bank_id="b1",
                       source="https://wiki.example.com/deploys", occurred_at=when)
    hit = await _hit(brain, "deploy freeze", "Thursdays")
    assert hit.source == "https://wiki.example.com/deploys" and hit.occurred_at == when


async def test_every_chunk_of_a_split_retain_carries_the_source(tmp_path):
    brain = _brain(tmp_path)
    await brain.retain(" ".join(f"word{i}" for i in range(600)), bank_id="b1", source="docs/long.md")
    items = await brain._pipeline.vector_store.list_vectors("b1", limit=100)  # noqa: SLF001
    assert len(items) > 1 and all(i.metadata[SOURCE_KEY] == "docs/long.md" for i in items)


async def test_without_a_source_nothing_is_added(tmp_path):
    brain = _brain(tmp_path)
    await brain.retain("We use httpx.", bank_id="b1")
    items = await brain._pipeline.vector_store.list_vectors("b1", limit=10)  # noqa: SLF001
    assert all(SOURCE_KEY not in (i.metadata or {}) for i in items)
    assert (await _hit(brain, "http client", "httpx")).source is None


def test_stored_source():
    assert stored_source({SOURCE_KEY: "a"}) == "a"
    assert stored_source(None) is None and stored_source({}) is None
    assert stored_source({SOURCE_KEY: ""}) is None and stored_source({SOURCE_KEY: 3}) is None
