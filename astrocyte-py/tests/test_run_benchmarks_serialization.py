from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SCRIPT_PATH = _REPO_ROOT / "scripts" / "run_benchmarks.py"
_SPEC = importlib.util.spec_from_file_location("run_benchmarks", _SCRIPT_PATH)
assert _SPEC and _SPEC.loader
_RUNNER = importlib.util.module_from_spec(_SPEC)
sys.modules["run_benchmarks"] = _RUNNER
_SPEC.loader.exec_module(_RUNNER)


def _metrics() -> SimpleNamespace:
    return SimpleNamespace(
        recall_precision=0.5,
        recall_hit_rate=0.75,
        recall_mrr=0.6,
        recall_ndcg=0.7,
        retain_latency_p50_ms=100.0,
        retain_latency_p95_ms=200.0,
        recall_latency_p50_ms=50.0,
        recall_latency_p95_ms=90.0,
        reflect_accuracy=0.5,
        total_tokens_used=1234,
        total_duration_seconds=12.3,
    )


def test_serialize_result_persists_per_question_and_failure_report() -> None:
    result = SimpleNamespace(
        overall_accuracy=0.5,
        category_accuracy={"multi-hop": 0.0, "temporal": 1.0},
        total_questions=2,
        correct=1,
        per_question=[
            {
                "question": "What did Alice do after moving?",
                "expected_answer": "She joined a startup",
                "category": "multi-hop",
                "evidence_ids": ["session_1"],
                "correct": False,
                "recall_hits": 10,
                "recall_top_hits": [
                    {"memory_id": "m1", "metadata": {"session_id": "session_1"}},
                ],
                "reflect_sources": [
                    {"memory_id": "m1", "metadata": {"session_id": "session_1"}},
                ],
                "reflect_answer_preview": "Alice moved to Boston.",
                "_precision": 0.0,
                "_reciprocal_rank": 0.0,
                "_latency_ms": 123.0,
                "_ndcg": 0.0,
                "_relevant_found": 0,
                "_evidence_id_hit": True,
            },
            {
                "question": "When did Bob visit?",
                "expected_answer": "June 2024",
                "category": "temporal",
                "correct": True,
                "recall_hits": 8,
                "reflect_answer_preview": "Bob visited in June 2024.",
                "_precision": 0.25,
                "_reciprocal_rank": 1.0,
                "_latency_ms": 99.0,
                "_ndcg": 1.0,
            },
        ],
        eval_result=SimpleNamespace(
            metrics=_metrics(),
            provider="pipeline",
            provider_tier="storage",
        ),
        canonical_f1_overall=None,
        canonical_f1_by_category={},
    )

    serialized = _RUNNER._serialize_result(result, "locomo", judge="canonical-llm")

    assert serialized["per_question"] == result.per_question
    assert serialized["failure_report"]["total_failed"] == 1
    assert serialized["failure_report"]["by_category"]["multi-hop"] == 1
    assert serialized["failure_report"]["failed_questions"] == [
        {
            "question": "What did Alice do after moving?",
            "expected_answer": "She joined a startup",
            "category": "multi-hop",
            "evidence_ids": ["session_1"],
            "recall_hits": 10,
            "recall_top_hits": [
                {"memory_id": "m1", "metadata": {"session_id": "session_1"}},
            ],
            "reflect_sources": [
                {"memory_id": "m1", "metadata": {"session_id": "session_1"}},
            ],
            "reflect_answer_preview": "Alice moved to Boston.",
            "_precision": 0.0,
            "_reciprocal_rank": 0.0,
            "_latency_ms": 123.0,
            "_ndcg": 0.0,
            "_relevant_found": 0,
            "_evidence_id_hit": True,
        }
    ]


def test_build_pipeline_brain_wires_wiki_and_entity_resolution(tmp_path: Path) -> None:
    config_path = tmp_path / "bench.yaml"
    config_path.write_text(
        "\n".join(
            [
                "provider_tier: storage",
                "vector_store: in_memory",
                "graph_store: in_memory",
                "wiki_store: in_memory",
                "llm_provider: mock",
                "wiki_compile:",
                "  enabled: true",
                "  auto_start: true",
                "entity_resolution:",
                "  enabled: true",
                "async_tasks:",
                "  enabled: true",
                "  backend: pgqueuer_in_memory",
                "  install_on_start: true",
                "  auto_start_worker: false",
            ]
        ),
        encoding="utf-8",
    )

    brain = _RUNNER._build_pipeline_brain(str(config_path))
    pipeline = getattr(brain, "_pipeline")

    assert getattr(brain, "_wiki_store") is not None
    assert pipeline.wiki_store is getattr(brain, "_wiki_store")
    assert pipeline.entity_resolver is not None
    assert getattr(brain, "_compile_queue") is not None
    assert brain.config.async_tasks.enabled is True
