"""The staleness benchmark's generator, scoring, and runner."""

from __future__ import annotations

import subprocess

import httpx
import pytest
from fastapi import FastAPI

from aml_selfeval.staleness import (
    Fact,
    build_repo,
    make_facts,
    run,
    score_fact,
    summarize,
)


def test_facts_are_deterministic_half_changed_and_unambiguous():
    a, b = make_facts(24, seed=7), make_facts(24, seed=7)
    assert a == b
    assert sum(f.new is not None for f in a) == 12
    values = [v for f in a for v in (f.old, f.new) if v]
    assert all(x == y or (x not in y and y not in x) for x in values for y in values)
    assert len({f.file for f in a}) == 24 and len({f.question for f in a}) == 24


def test_repo_has_old_values_then_silently_changed_ones(tmp_path):
    facts = make_facts(4, seed=1)
    first, head = build_repo(tmp_path / "r", facts)
    def show(sha: str, f) -> str:
        return subprocess.run(
            ["git", "-C", str(tmp_path / "r"), "show", f"{sha}:{f.file}"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout

    for f in facts:
        assert f.old in show(first, f)
        assert (f.new or f.old) in show(head, f)


@pytest.mark.parametrize(
    ("new", "items", "expected"),
    [
        ("222", [{"content": "x is 111"}], "stale_unflagged"),
        ("222", [{"content": "x is 111", "stale": True}], "flagged"),
        ("222", [{"content": "x is 111 (may be stale: file changed)"}], "flagged"),
        ("222", [{"content": "x is 111"}, {"content": "now 222"}], "current"),
        ("222", [{"content": "unrelated"}], "missing"),
        (None, [{"content": "x is 111"}], "recalled"),
        (None, [], "missing"),
    ],
)
def test_score_fact(new, items, expected):
    assert score_fact(Fact("f", "x", "q?", "111", new), items) == expected


def test_summary_rates_and_intervals():
    rows = [{"changed": True, "outcome": "stale_unflagged"}] * 3 + [
        {"changed": False, "outcome": "recalled"}
    ] * 2
    s = summarize(rows)
    assert s["changed"]["stale_unflagged"]["rate"] == 1.0
    assert s["control"]["recalled"]["k"] == 2
    lo, hi = s["changed"]["stale_unflagged"]["ci95"]
    assert 0.0 < lo < 1.0 and hi == 1.0


def _echo_memory(flag_stale: bool) -> FastAPI:
    """A memory that stores every message and returns all of them on search,
    optionally flagging everything stale: the two extremes of the metric."""
    app = FastAPI()
    store: dict[str, list[str]] = {}
    seen_context: list[dict] = []

    @app.post("/add")
    async def add(body: dict):
        seen_context.append(body.get("context", {}))
        store.setdefault(body["user_id"], []).extend(
            m["content"] for m in body["messages"]
        )
        return {
            "success": True,
            "request_id": body["request_id"],
            "user_id": body["user_id"],
            "session_id": body["session_id"],
        }

    @app.post("/search")
    async def search(body: dict):
        seen_context.append(body.get("context", {}))
        return {
            "data": [
                {"id": str(i), "content": t, "stale": flag_stale}
                for i, t in enumerate(store.get(body["user_id"], []))
            ]
        }

    app.state.seen_context = seen_context
    return app


@pytest.mark.asyncio
async def test_a_memory_without_anchors_serves_every_changed_fact_stale(tmp_path):
    app = _echo_memory(flag_stale=False)
    report = await run(
        "http://t",
        n=8,
        seed=3,
        run_id="r",
        workdir=tmp_path,
        transport=httpx.ASGITransport(app=app),
    )
    assert report["summary"]["changed"]["stale_unflagged"]["rate"] == 1.0
    assert report["summary"]["control"]["recalled"]["rate"] == 1.0
    contexts = app.state.seen_context
    assert all(c.get("repo") for c in contexts), "systems receive the repository"
    assert contexts[0]["head"] != contexts[-1]["head"], "searches see the changed HEAD"


@pytest.mark.asyncio
async def test_flagging_stale_items_moves_them_out_of_stale_unflagged(tmp_path):
    app = _echo_memory(flag_stale=True)
    report = await run(
        "http://t",
        n=8,
        seed=3,
        run_id="r",
        workdir=tmp_path,
        transport=httpx.ASGITransport(app=app),
    )
    assert report["summary"]["changed"]["flagged"]["rate"] == 1.0
    assert report["summary"]["changed"]["stale_unflagged"]["k"] == 0
