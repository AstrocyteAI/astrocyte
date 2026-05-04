from __future__ import annotations

import json
from pathlib import Path

from astrocyte.config import _dict_to_config, load_config

_BENCHMARKS_DIR = Path(__file__).resolve().parent.parent / "benchmarks"


def test_reference_stack_config_parses_wiki_compile_and_entity_resolution() -> None:
    config = _dict_to_config(
        {
            "provider_tier": "storage",
            "vector_store": "postgres",
            "graph_store": "age",
            "wiki_store": "postgres",
            "wiki_compile": {
                "enabled": True,
                "auto_start": True,
                "size_threshold": 25,
            },
            "entity_resolution": {
                "enabled": True,
                "similarity_threshold": 0.7,
                "confirmation_threshold": 0.9,
                "max_candidates_per_entity": 2,
            },
            "async_tasks": {
                "enabled": True,
                "backend": "pgqueuer",
                "install_on_start": True,
                "auto_start_worker": True,
            },
        }
    )

    assert config.wiki_store == "postgres"
    assert config.wiki_compile.enabled is True
    assert config.wiki_compile.auto_start is True
    assert config.wiki_compile.size_threshold == 25
    assert config.entity_resolution.enabled is True
    assert config.entity_resolution.similarity_threshold == 0.7
    assert config.entity_resolution.confirmation_threshold == 0.9
    assert config.entity_resolution.max_candidates_per_entity == 2
    assert config.async_tasks.enabled is True
    assert config.async_tasks.backend == "pgqueuer"
    assert config.async_tasks.install_on_start is True
    assert config.async_tasks.auto_start_worker is True


def test_named_benchmark_presets_parse(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")

    expected = {
        "config-baseline.yaml": {
            "sfe": False,
            "semantic_links": False,
            "agentic": False,
            "cross_encoder": False,
        },
        "config-fast-recall.yaml": {
            "sfe": True,
            "semantic_links": True,
            "agentic": False,
            # CE turned on for fast-recall as part of the post-rerank-fault
            # analysis (commits up to 95b297b) — top_k=12 matches the low-budget
            # candidate_limit so it reranks exactly the synthesis pool with no
            # over-fetch latency penalty.
            "cross_encoder": True,
        },
        "config-hindsight-parity.yaml": {
            "sfe": True,
            "semantic_links": True,
            "agentic": True,
            "cross_encoder": True,
        },
        "config-hindsight-balanced.yaml": {
            "sfe": True,
            "semantic_links": True,
            "agentic": True,
            "cross_encoder": True,
        },
        "config-quality-max.yaml": {
            "sfe": True,
            "semantic_links": True,
            "agentic": True,
            "cross_encoder": True,
        },
    }

    for filename, flags in expected.items():
        config = load_config(_BENCHMARKS_DIR / filename)
        assert config.provider_tier == "storage"
        assert config.vector_store == "postgres"
        assert config.document_store == "postgres"
        assert config.structured_fact_extraction.enabled is flags["sfe"]
        assert config.semantic_link_graph.enabled is flags["semantic_links"]
        assert config.agentic_reflect.enabled is flags["agentic"]
        assert config.cross_encoder_rerank.enabled is flags["cross_encoder"]
        assert config.benchmark_preset.name == filename.removeprefix("config-").removesuffix(".yaml")
        assert config.benchmark_preset.version == 1
        assert getattr(config.benchmark_preset, config.benchmark_preset.budget).max_tokens > 0


def test_ablation_matrix_references_existing_presets() -> None:
    matrix = json.loads((_BENCHMARKS_DIR / "ablation-matrix.json").read_text())
    scenario_ids = {scenario["id"] for scenario in matrix["scenarios"]}

    assert {"baseline", "fast-recall", "hindsight-parity", "hindsight-balanced", "quality-max"} <= scenario_ids
    for scenario in matrix["scenarios"]:
        assert (_BENCHMARKS_DIR / scenario["config"]).exists()


def test_default_config_matches_fast_recall(monkeypatch) -> None:
    """Lock in the v1 default decision: benchmarks/config.yaml MUST stay
    aligned with config-fast-recall.yaml.

    Background: on 2026-05-03 the five-preset ablation matrix selected
    fast-recall as v1 (Pareto winner — 51.5% LoCoMo overall, $0.0035/q,
    beating hindsight-parity's 50.5% at $0.0085/q). See
    docs/_plugins/benchmark-presets.md.

    If you intentionally change the default preset, update this test AND
    the decision record in docs/_plugins/benchmark-presets.md so the
    reasoning is preserved.
    """
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")

    default = load_config(_BENCHMARKS_DIR / "config.yaml")
    fast_recall = load_config(_BENCHMARKS_DIR / "config-fast-recall.yaml")

    # Preset identity: default config MUST declare itself as fast-recall.
    assert default.benchmark_preset.name == "fast-recall"
    assert default.benchmark_preset.version == fast_recall.benchmark_preset.version
    assert default.benchmark_preset.budget == fast_recall.benchmark_preset.budget

    # Behavior-shaping flags MUST match fast-recall (this is the bundle the
    # Pareto-winner result actually measured).
    assert default.structured_fact_extraction.enabled is fast_recall.structured_fact_extraction.enabled
    assert default.semantic_link_graph.enabled is fast_recall.semantic_link_graph.enabled
    assert default.agentic_reflect.enabled is fast_recall.agentic_reflect.enabled
    assert default.cross_encoder_rerank.enabled is fast_recall.cross_encoder_rerank.enabled
    assert default.adversarial_defense.abstention_enabled is fast_recall.adversarial_defense.abstention_enabled
    assert default.adversarial_defense.adversarial_prompt_enabled is fast_recall.adversarial_defense.adversarial_prompt_enabled
    assert default.adversarial_defense.premise_verification_enabled is fast_recall.adversarial_defense.premise_verification_enabled
    assert default.causal_links.enabled is fast_recall.causal_links.enabled
    assert default.wiki_compile.enabled is fast_recall.wiki_compile.enabled
    assert default.query_analyzer.enabled is fast_recall.query_analyzer.enabled
