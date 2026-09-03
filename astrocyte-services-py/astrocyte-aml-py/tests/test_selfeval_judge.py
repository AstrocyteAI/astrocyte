"""Tests for the answer/judge driver.

Hermetic: a synthetic stand-in for AML's pipeline module provides the
prompt/parse functions, and a stub ASGI app provides the LLM endpoint.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from aml_selfeval.judge import complete, load_pipeline, run_answer, run_evaluate

FAKE_PIPELINE = '''
from api_config import ANSWER_MODEL  # pipelines import this at module scope

def render_answer_prompt(item):
    return "ANSWER:" + item["question"] + "|" + item.get("retrieved_context", "")

def render_accuracy_prompt(item, generated_answer):
    return "JUDGE:" + item["gold_answer"] + "|" + generated_answer

def parse_judge_label(response):
    if "CORRECT" in response:
        return "CORRECT"
    if "WRONG" in response:
        return "WRONG"
    raise ValueError("judge label must be CORRECT or WRONG")
'''


@pytest.fixture
def aml_repo(tmp_path: Path) -> Path:
    """A minimal stand-in for a clone of AML's repo."""
    (tmp_path / "api_config.py").write_text('ANSWER_MODEL = ""\n')
    bench = tmp_path / "data" / "longmemeval-s"
    bench.mkdir(parents=True)
    (bench / "pipeline.py").write_text(FAKE_PIPELINE)
    return tmp_path


def _llm_app(reply: str = "the answer") -> FastAPI:
    app = FastAPI()
    seen: list[dict] = []
    app.state.seen = seen

    @app.post("/v1/chat/completions")
    async def chat(body: dict) -> dict:
        seen.append(body)
        prompt = body["messages"][0]["content"]
        text = "verdict CORRECT" if prompt.startswith("JUDGE:") else reply
        return {"choices": [{"message": {"role": "assistant", "content": text}}]}

    return app


def _args(tmp_path: Path, **over) -> argparse.Namespace:
    ns = argparse.Namespace(
        input=str(tmp_path / "input.jsonl"),
        output=str(tmp_path / "answers.jsonl"),
        scored=str(tmp_path / "scored.jsonl"),
        answer_base="http://test/v1", answer_model="m", answer_key="k",
        judge_base="http://test/v1", judge_model="m", judge_key="k",
        concurrency=2, timeout=30.0, resume=False,
    )
    for k, v in over.items():
        setattr(ns, k, v)
    return ns


def _write(path: Path, rows: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")


def _route_httpx_to(app: FastAPI, monkeypatch) -> None:
    """Make the driver's own ``httpx.AsyncClient(...)`` talk to ``app``.

    The original class is captured first — building the replacement from the
    patched name would recurse.
    """
    original = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kw: original(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ),
    )


class TestLoadPipeline:
    def test_imports_amls_prompt_functions(self, aml_repo):
        mod = load_pipeline(aml_repo, "longmemeval-s")
        assert mod.render_answer_prompt({"question": "Q", "retrieved_context": "C"}) == "ANSWER:Q|C"
        assert mod.parse_judge_label("CORRECT") == "CORRECT"

    def test_missing_bench_fails_loudly(self, aml_repo):
        with pytest.raises(SystemExit, match="no pipeline at"):
            load_pipeline(aml_repo, "does-not-exist")


class TestCompleteRequestShape:
    @pytest.mark.asyncio
    async def test_sends_exactly_what_amls_own_complete_sends(self):
        app = _llm_app()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as c:
            out = await complete(c, "http://test/v1", "secret", "mymodel", "hello")
        assert out == "the answer"
        body = app.state.seen[0]
        assert body == {
            "model": "mymodel",
            "messages": [{"role": "user", "content": "hello"}],
            "temperature": 0,
        }


class TestAnswerAndEvaluate:
    @pytest.mark.asyncio
    async def test_end_to_end_produces_scored_rows(self, aml_repo, tmp_path, monkeypatch):
        pipeline = load_pipeline(aml_repo, "longmemeval-s")
        _write(tmp_path / "input.jsonl", [
            {"id": "q1", "question": "Q1", "gold_answer": "G1", "retrieved_context": "C1"},
            {"id": "q2", "question": "Q2", "gold_answer": "G2", "retrieved_context": "C2"},
        ])
        app = _llm_app()
        _route_httpx_to(app, monkeypatch)
        args = _args(tmp_path)
        await run_answer(pipeline, args)
        await run_evaluate(pipeline, args)

        answers = [json.loads(x) for x in (tmp_path / "answers.jsonl").read_text().splitlines()]
        scored = [json.loads(x) for x in (tmp_path / "scored.jsonl").read_text().splitlines()]
        assert {a["id"] for a in answers} == {"q1", "q2"}
        assert all(s["is_correct"] for s in scored)
        assert len(scored) == 2

    @pytest.mark.asyncio
    async def test_retrieved_context_reaches_the_answer_prompt(
        self, aml_repo, tmp_path, monkeypatch
    ):
        """The whole point: our memories must land in AML's prompt."""
        pipeline = load_pipeline(aml_repo, "longmemeval-s")
        _write(tmp_path / "input.jsonl",
               [{"id": "q1", "question": "Q", "gold_answer": "G",
                 "retrieved_context": "- Rex is a beagle"}])
        app = _llm_app()
        _route_httpx_to(app, monkeypatch)
        await run_answer(pipeline, _args(tmp_path))
        assert "Rex is a beagle" in app.state.seen[0]["messages"][0]["content"]

    @pytest.mark.asyncio
    async def test_missing_answers_are_skipped_not_fatal(
        self, aml_repo, tmp_path, monkeypatch, capsys
    ):
        """AML hard-fails on ID mismatch; an expensive run should survive it."""
        pipeline = load_pipeline(aml_repo, "longmemeval-s")
        _write(tmp_path / "input.jsonl", [
            {"id": "q1", "question": "Q1", "gold_answer": "G1"},
            {"id": "q2", "question": "Q2", "gold_answer": "G2"},
        ])
        _write(tmp_path / "answers.jsonl", [{"id": "q1", "generated_answer": "A1"}])
        app = _llm_app()
        _route_httpx_to(app, monkeypatch)
        await run_evaluate(pipeline, _args(tmp_path))
        scored = [json.loads(x) for x in (tmp_path / "scored.jsonl").read_text().splitlines()]
        assert [s["id"] for s in scored] == ["q1"]
        assert "1 items have no answer" in capsys.readouterr().err
