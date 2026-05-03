from __future__ import annotations

import json
from pathlib import Path

from astrocyte.config import _dict_to_config, load_config

_BENCHMARKS_DIR = Path(__file__).resolve().parent.parent / "benchmarks"


def test_reference_stack_config_parses_wiki_compile_and_entity_resolution() -> None:
    config = _dict_to_config(
        {
            "provider_tier": "storage",
            "vector_store": "pgvector",
            "graph_store": "age",
            "wiki_store": "pgvector",
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

    assert config.wiki_store == "pgvector"
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
            "cross_encoder": False,
        },
        "config-hindsight-parity.yaml": {
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
        assert config.vector_store == "pgvector"
        assert config.document_store == "pgvector"
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

    assert {"baseline", "fast-recall", "hindsight-parity", "quality-max"} <= scenario_ids
    for scenario in matrix["scenarios"]:
        assert (_BENCHMARKS_DIR / scenario["config"]).exists()
