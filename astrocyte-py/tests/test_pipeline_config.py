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


def test_store_side_dedup_is_on_by_default_and_can_be_turned_off(tmp_path) -> None:
    from astrocyte.config import load_config

    orch = _orch()
    orch.apply_config(PipelineConfig.from_config(AstrocyteConfig()))
    assert orch.dedup_consult_store is True

    path = tmp_path / "astrocyte.yaml"
    path.write_text("signal_quality:\n  dedup:\n    consult_store: false\n")
    orch.apply_config(PipelineConfig.from_config(load_config(str(path))))
    assert orch.dedup_consult_store is False


def test_default_dedup_config_matches_the_old_hardcoded_detector() -> None:
    """The orchestrator used to build ``DedupDetector(similarity_threshold=0.95)``
    and ignore ``signal_quality.dedup``. Wiring the block must not move a
    default config: dedup on, threshold 0.95."""
    orch = _orch()
    assert (orch.dedup_enabled, orch.dedup_similarity_threshold) == (True, 0.95)
    orch.apply_config(PipelineConfig.from_config(AstrocyteConfig()))
    assert (orch.dedup_enabled, orch.dedup_similarity_threshold) == (True, 0.95)
    assert orch._dedup.threshold == 0.95


@pytest.mark.parametrize(
    ("profile", "enabled", "threshold"),
    [
        ("minimal", False, 0.95),
        ("coding", True, 0.98),
        ("research", True, 0.97),
        ("personal", True, 0.93),
        ("support", True, 0.92),
    ],
)
def test_profile_dedup_settings_reach_the_orchestrator(tmp_path, profile, enabled, threshold) -> None:
    from astrocyte.config import load_config

    path = tmp_path / "astrocyte.yaml"
    path.write_text(f"profile: {profile}\n")
    orch = _orch()
    orch.apply_config(PipelineConfig.from_config(load_config(str(path))))
    assert orch.dedup_enabled is enabled
    assert orch.dedup_similarity_threshold == threshold
    assert orch._dedup.threshold == threshold


def _near_duplicate_llm(monkeypatch: pytest.MonkeyPatch, cosine: float) -> MockLLMProvider:
    """An LLM whose two test sentences embed at exactly ``cosine`` similarity."""
    import math

    vectors = {
        "Deploys happen on Tuesdays.": [1.0, 0.0],
        "Deploys go out on Tuesdays.": [cosine, math.sqrt(1 - cosine**2)],
    }
    llm = MockLLMProvider()

    async def embed(texts, **_kw):
        return [vectors[t] for t in texts]

    monkeypatch.setattr(llm, "embed", embed)
    return llm


async def _retain_pair(orch: PipelineOrchestrator, mip_pipeline=None):
    from astrocyte.types import RetainRequest

    await orch.retain(RetainRequest(content="Deploys happen on Tuesdays.", bank_id="b1"))
    return await orch.retain(
        RetainRequest(content="Deploys go out on Tuesdays.", bank_id="b1", mip_pipeline=mip_pipeline)
    )


@pytest.mark.asyncio
async def test_config_threshold_decides_retain_dedup(monkeypatch) -> None:
    """A 0.96 pair is a duplicate at the default 0.95 and kept under the coding
    profile's 0.98 — the threshold that profile documents."""
    default = PipelineOrchestrator(InMemoryVectorStore(), _near_duplicate_llm(monkeypatch, 0.96))
    assert (await _retain_pair(default)).deduplicated is True

    config = AstrocyteConfig()
    config.signal_quality.dedup.similarity_threshold = 0.98
    strict = PipelineOrchestrator(InMemoryVectorStore(), _near_duplicate_llm(monkeypatch, 0.96))
    strict.apply_config(PipelineConfig.from_config(config))
    assert (await _retain_pair(strict)).stored is True


@pytest.mark.asyncio
async def test_mip_dedup_threshold_still_overrides_config(monkeypatch) -> None:
    from astrocyte.mip.schema import DedupSpec, PipelineSpec

    config = AstrocyteConfig()
    config.signal_quality.dedup.similarity_threshold = 0.98
    orch = PipelineOrchestrator(InMemoryVectorStore(), _near_duplicate_llm(monkeypatch, 0.96))
    orch.apply_config(PipelineConfig.from_config(config))
    r = await _retain_pair(orch, PipelineSpec(version=1, dedup=DedupSpec(threshold=0.9)))
    assert r.deduplicated is True


@pytest.mark.asyncio
async def test_disabled_dedup_keeps_exact_duplicates(monkeypatch) -> None:
    """``enabled: false`` (the minimal profile) turns retain-time dedup off —
    the cache check, the store check, and MIP ``dedup`` rules alike."""
    from astrocyte.mip.schema import DedupSpec, PipelineSpec
    from astrocyte.types import RetainRequest

    config = AstrocyteConfig()
    config.signal_quality.dedup.enabled = False
    vs = InMemoryVectorStore()
    orch = PipelineOrchestrator(vs, MockLLMProvider())
    orch.apply_config(PipelineConfig.from_config(config))
    request = RetainRequest(content="Deploys happen on Tuesdays.", bank_id="b1")
    assert (await orch.retain(request)).stored is True
    assert (await orch.retain(request)).stored is True
    mip = PipelineSpec(version=1, dedup=DedupSpec(threshold=0.5, action="skip"))
    assert (await orch.retain(RetainRequest(content=request.content, bank_id="b1", mip_pipeline=mip))).stored is True
    assert len(await vs.list_vectors("b1")) == 3


def _load(tmp_path, text: str) -> AstrocyteConfig:
    from astrocyte.config import load_config

    path = tmp_path / "astrocyte.yaml"
    path.write_text(text)
    return load_config(str(path))


def test_bank_signal_quality_inherits_unset_keys_from_top_level(tmp_path) -> None:
    config = _load(
        tmp_path,
        "signal_quality:\n  dedup:\n    enabled: false\n    consult_store: false\n"
        "banks:\n"
        "  strict:\n    signal_quality:\n      dedup:\n        similarity_threshold: 0.99\n"
        "  loose:\n    signal_quality:\n      dedup:\n        enabled: true\n"
        "  plain:\n    access: []\n",
    )
    strict = config.banks["strict"].signal_quality.dedup
    assert (strict.enabled, strict.similarity_threshold, strict.consult_store) == (False, 0.99, False)
    loose = config.banks["loose"].signal_quality.dedup
    assert (loose.enabled, loose.similarity_threshold, loose.consult_store) == (True, 0.95, False)
    assert config.banks["plain"].signal_quality is None

    cfg = PipelineConfig.from_config(config)
    assert set(cfg.dedup_by_bank) == {"strict", "loose"}
    assert cfg.dedup_by_bank["strict"] is strict


def test_bank_signal_quality_without_top_level_block_uses_defaults(tmp_path) -> None:
    config = _load(tmp_path, "banks:\n  b1:\n    signal_quality:\n      dedup:\n        consult_store: false\n")
    dedup = config.banks["b1"].signal_quality.dedup
    assert (dedup.enabled, dedup.similarity_threshold, dedup.consult_store) == (True, 0.95, False)


def test_profile_dedup_is_inherited_by_bank_overrides(tmp_path) -> None:
    """The profile merges into the top-level block first, so a bank override
    under ``profile: minimal`` stays off unless it turns dedup back on."""
    config = _load(
        tmp_path,
        "profile: minimal\nbanks:\n  b1:\n    signal_quality:\n      dedup:\n        similarity_threshold: 0.9\n",
    )
    assert config.banks["b1"].signal_quality.dedup.enabled is False


def test_no_bank_overrides_means_empty_map() -> None:
    assert PipelineConfig.from_config(AstrocyteConfig()).dedup_by_bank == {}


def _with_banks(orch: PipelineOrchestrator, tmp_path, banks_yaml: str, top: str = "") -> PipelineOrchestrator:
    orch.apply_config(PipelineConfig.from_config(_load(tmp_path, top + "banks:\n" + banks_yaml)))
    return orch


@pytest.mark.asyncio
async def test_bank_threshold_applies_only_to_that_bank(monkeypatch, tmp_path) -> None:
    from astrocyte.types import RetainRequest

    orch = _with_banks(
        PipelineOrchestrator(InMemoryVectorStore(), _near_duplicate_llm(monkeypatch, 0.96)),
        tmp_path,
        "  code:\n    signal_quality:\n      dedup:\n        similarity_threshold: 0.98\n",
    )
    for bank in ("code", "chat"):
        await orch.retain(RetainRequest(content="Deploys happen on Tuesdays.", bank_id=bank))
    code = await orch.retain(RetainRequest(content="Deploys go out on Tuesdays.", bank_id="code"))
    chat = await orch.retain(RetainRequest(content="Deploys go out on Tuesdays.", bank_id="chat"))
    assert code.stored is True, "0.96 < the bank's 0.98"
    assert chat.deduplicated is True, "0.96 >= the top-level 0.95"


@pytest.mark.asyncio
async def test_mip_threshold_beats_bank_threshold(monkeypatch, tmp_path) -> None:
    from astrocyte.mip.schema import DedupSpec, PipelineSpec

    orch = _with_banks(
        PipelineOrchestrator(InMemoryVectorStore(), _near_duplicate_llm(monkeypatch, 0.96)),
        tmp_path,
        "  b1:\n    signal_quality:\n      dedup:\n        similarity_threshold: 0.98\n",
    )
    r = await _retain_pair(orch, PipelineSpec(version=1, dedup=DedupSpec(threshold=0.9)))
    assert r.deduplicated is True


@pytest.mark.asyncio
async def test_bank_can_turn_dedup_off_or_on_against_top_level(tmp_path) -> None:
    from astrocyte.types import RetainRequest

    orch = _with_banks(
        PipelineOrchestrator(InMemoryVectorStore(), MockLLMProvider()),
        tmp_path,
        "  raw:\n    signal_quality:\n      dedup:\n        enabled: false\n",
    )
    raw = RetainRequest(content="Deploys happen on Tuesdays.", bank_id="raw")
    other = RetainRequest(content="Deploys happen on Tuesdays.", bank_id="other")
    assert [(await orch.retain(raw)).stored for _ in range(2)] == [True, True]
    assert [(await orch.retain(other)).stored for _ in range(2)] == [True, False]

    on = _with_banks(
        PipelineOrchestrator(InMemoryVectorStore(), MockLLMProvider()),
        tmp_path,
        "  curated:\n    signal_quality:\n      dedup:\n        enabled: true\n",
        top="signal_quality:\n  dedup:\n    enabled: false\n",
    )
    curated = RetainRequest(content="Deploys happen on Tuesdays.", bank_id="curated")
    assert [(await on.retain(curated)).stored for _ in range(2)] == [True, False]


@pytest.mark.asyncio
async def test_bank_consult_store_controls_the_cross_process_check(tmp_path) -> None:
    from astrocyte.types import RetainRequest

    vs = InMemoryVectorStore()
    banks = "  cache_only:\n    signal_quality:\n      dedup:\n        consult_store: false\n"
    first = _with_banks(PipelineOrchestrator(vs, MockLLMProvider()), tmp_path, banks)
    for bank in ("cache_only", "default"):
        await first.retain(RetainRequest(content="Deploys happen on Tuesdays.", bank_id=bank))

    # A fresh orchestrator has an empty cache: only the store check can match.
    fresh = _with_banks(PipelineOrchestrator(vs, MockLLMProvider()), tmp_path, banks)
    cache_only = await fresh.retain(RetainRequest(content="Deploys happen on Tuesdays.", bank_id="cache_only"))
    default = await fresh.retain(RetainRequest(content="Deploys happen on Tuesdays.", bank_id="default"))
    assert cache_only.stored is True
    assert default.deduplicated is True


_TWO_CHUNKS = "Deploys happen on Tuesdays. The cache TTL is five minutes."  # two chunks at max 30


async def _retain_with_one_duplicate_chunk(orch: PipelineOrchestrator, bank_id: str = "b1", mip_pipeline=None):
    """Store chunk one, then retain chunk one + a new chunk two."""
    from astrocyte.types import RetainRequest

    await orch.retain(RetainRequest(content="Deploys happen on Tuesdays.", bank_id=bank_id))
    return await orch.retain(RetainRequest(content=_TWO_CHUNKS, bank_id=bank_id, mip_pipeline=mip_pipeline))


def _orch_with(tmp_path, yaml_text: str, vs: InMemoryVectorStore | None = None) -> PipelineOrchestrator:
    orch = PipelineOrchestrator(vs or InMemoryVectorStore(), MockLLMProvider(), max_chunk_size=30)
    orch.apply_config(PipelineConfig.from_config(_load(tmp_path, yaml_text)))
    return orch


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("action", "stored", "rows"),
    [
        ("skip_chunk", True, 2),  # duplicate chunk dropped, new chunk kept
        ("update", True, 2),  # not implemented: behaves as skip_chunk
        ("skip", False, 1),  # whole retain rejected
        ("warn", True, 3),  # duplicate kept too
    ],
)
async def test_config_dedup_action_decides_what_a_duplicate_does(tmp_path, action, stored, rows) -> None:
    vs = InMemoryVectorStore()
    orch = _orch_with(tmp_path, f"signal_quality:\n  dedup:\n    action: {action}\n", vs)
    r = await _retain_with_one_duplicate_chunk(orch)
    assert r.stored is stored
    assert len(await vs.list_vectors("b1")) == rows


@pytest.mark.asyncio
async def test_default_dedup_action_is_skip_chunk(tmp_path) -> None:
    vs = InMemoryVectorStore()
    orch = PipelineOrchestrator(vs, MockLLMProvider(), max_chunk_size=30)
    assert orch.dedup_action == "skip_chunk"
    orch.apply_config(PipelineConfig.from_config(AstrocyteConfig()))
    assert orch.dedup_action == "skip_chunk"
    assert (await _retain_with_one_duplicate_chunk(orch)).stored is True
    assert len(await vs.list_vectors("b1")) == 2


@pytest.mark.asyncio
async def test_mip_dedup_action_overrides_config_action(tmp_path) -> None:
    from astrocyte.mip.schema import DedupSpec, PipelineSpec

    vs = InMemoryVectorStore()
    orch = _orch_with(tmp_path, "signal_quality:\n  dedup:\n    action: skip\n", vs)
    r = await _retain_with_one_duplicate_chunk(
        orch, mip_pipeline=PipelineSpec(version=1, dedup=DedupSpec(action="warn"))
    )
    assert r.stored is True
    assert len(await vs.list_vectors("b1")) == 3


@pytest.mark.asyncio
async def test_mip_rule_without_action_falls_back_to_config_action(tmp_path) -> None:
    from astrocyte.mip.schema import DedupSpec, PipelineSpec

    orch = _orch_with(tmp_path, "signal_quality:\n  dedup:\n    action: skip\n")
    r = await _retain_with_one_duplicate_chunk(
        orch, mip_pipeline=PipelineSpec(version=1, dedup=DedupSpec(threshold=0.95))
    )
    assert r.stored is False and r.deduplicated is True


@pytest.mark.asyncio
async def test_bank_dedup_action_applies_only_to_that_bank(tmp_path) -> None:
    vs = InMemoryVectorStore()
    orch = _orch_with(tmp_path, "banks:\n  strict:\n    signal_quality:\n      dedup:\n        action: skip\n", vs)
    assert (await _retain_with_one_duplicate_chunk(orch, "strict")).stored is False
    assert (await _retain_with_one_duplicate_chunk(orch, "other")).stored is True


@pytest.mark.asyncio
async def test_config_dedup_action_applies_in_retain_many(tmp_path) -> None:
    from astrocyte.types import RetainRequest

    vs = InMemoryVectorStore()
    orch = _orch_with(tmp_path, "signal_quality:\n  dedup:\n    action: skip\n", vs)
    await orch.retain(RetainRequest(content="Deploys happen on Tuesdays.", bank_id="b1"))
    results = await orch.retain_many([RetainRequest(content=_TWO_CHUNKS, bank_id="b1")])
    assert results[0].stored is False and results[0].deduplicated is True
    assert len(await vs.list_vectors("b1")) == 1


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
