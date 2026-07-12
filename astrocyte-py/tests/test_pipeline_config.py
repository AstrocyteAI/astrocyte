"""Characterization tests for the PipelineConfig two-phase-mutation refactor.

These pin the invariant that ``PipelineConfig.from_config`` +
``PipelineOrchestrator.apply_config`` reproduce exactly what the old inline
``Astrocyte.set_pipeline`` body used to poke onto the orchestrator — the
derivation moved, the resulting flag values did not.
"""

from __future__ import annotations

import dataclasses

import pytest

from astrocyte.config import AstrocyteConfig
from astrocyte.pipeline.orchestrator import PipelineOrchestrator
from astrocyte.pipeline.pipeline_config import PipelineConfig, _temporal_expansion_flag
from astrocyte.testing.in_memory import InMemoryVectorStore, MockLLMProvider


def _orch() -> PipelineOrchestrator:
    return PipelineOrchestrator(
        vector_store=InMemoryVectorStore(),
        llm_provider=MockLLMProvider(),
        enable_observation_consolidation=False,
    )


def test_every_config_field_maps_to_an_orchestrator_attribute() -> None:
    """apply_config must never silently drop a flag: every PipelineConfig field
    has to correspond to a real orchestrator attribute (else the old behaviour
    of that flag is lost). apply_config raises AttributeError on drift; this
    asserts a default config applies cleanly."""
    cfg = PipelineConfig.from_config(AstrocyteConfig())
    orch = _orch()
    orch.apply_config(cfg)  # must not raise
    for field in dataclasses.fields(cfg):
        assert hasattr(orch, field.name)
        assert getattr(orch, field.name) == getattr(cfg, field.name)


def test_apply_config_rejects_unknown_field() -> None:
    """A config field with no orchestrator attribute is a loud failure."""
    cfg = PipelineConfig.from_config(AstrocyteConfig())
    bad = dataclasses.replace(cfg)  # same fields
    orch = _orch()
    # Simulate drift by feeding apply_config a config-like object with a stray key.
    object.__setattr__(bad, "__dict__", {**bad.__dict__, "nonexistent_flag": 1})
    with pytest.raises(AttributeError, match="nonexistent_flag"):
        orch.apply_config(bad)


def test_disabled_features_resolve_to_none_or_defaults() -> None:
    cfg = PipelineConfig.from_config(AstrocyteConfig())
    # Opt-in features are off by default → their handles are None.
    assert cfg.cross_encoder is None
    assert cfg.link_expansion_params is None
    assert cfg.agentic_reflect_params is None
    assert cfg.mental_model_service is None
    assert cfg.causal_links_enabled is False


def test_enabled_features_construct_handles() -> None:
    config = AstrocyteConfig()
    config.spreading_activation.enabled = True
    config.agentic_reflect.enabled = True
    cfg = PipelineConfig.from_config(config)
    assert cfg.link_expansion_params is not None
    assert cfg.agentic_reflect_params is not None
    # Values flow through from the config block.
    assert cfg.agentic_reflect_params.max_iterations == config.agentic_reflect.max_iterations


def test_source_store_and_mental_model_store_thread_through() -> None:
    sentinel_source = object()
    sentinel_mm = object()
    cfg = PipelineConfig.from_config(
        AstrocyteConfig(),
        source_store=sentinel_source,
        mental_model_store=sentinel_mm,
    )
    assert cfg.source_store is sentinel_source
    assert cfg.mental_model_service is not None  # service wraps the store


@pytest.mark.parametrize(
    ("env", "config_default", "expected"),
    [
        ("1", False, True),
        ("true", False, True),
        ("yes", False, True),
        ("0", True, False),
        ("false", True, False),
        ("no", True, False),
        ("", True, True),  # unset → config wins
        ("", False, False),
        ("garbage", True, True),  # unrecognised → config wins
    ],
)
def test_temporal_expansion_env_override(
    monkeypatch: pytest.MonkeyPatch, env: str, config_default: bool, expected: bool
) -> None:
    if env:
        monkeypatch.setenv("ASTROCYTE_M18_ENABLE_TEMPORAL_EXPANSION", env)
    else:
        monkeypatch.delenv("ASTROCYTE_M18_ENABLE_TEMPORAL_EXPANSION", raising=False)
    assert _temporal_expansion_flag(config_default) is expected
