from __future__ import annotations

from astrocyte.config import _dict_to_config


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
