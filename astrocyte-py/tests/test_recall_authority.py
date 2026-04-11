"""M7 — recall authority formatter and config validation."""

from __future__ import annotations

import pytest

from astrocyte.config import (
    AstrocyteConfig,
    RecallAuthorityConfig,
    RecallAuthorityTierConfig,
    load_config,
    validate_astrocyte_config,
)
from astrocyte.errors import ConfigError
from astrocyte.recall.authority import apply_recall_authority, build_authority_context
from astrocyte.types import MemoryHit, RecallResult


def test_build_authority_context_buckets_and_unassigned() -> None:
    cfg = RecallAuthorityConfig(
        enabled=True,
        rules_inline="Follow org policy.",
        tiers=[
            RecallAuthorityTierConfig(id="canonical", priority=1, label="Canonical"),
            RecallAuthorityTierConfig(id="notes", priority=2, label="Notes"),
        ],
    )
    hits = [
        MemoryHit(text="A", score=1.0, metadata={"authority_tier": "notes"}),
        MemoryHit(text="B", score=0.9, metadata={"authority_tier": "canonical"}),
        MemoryHit(text="C", score=0.8, metadata={}),
    ]
    text = build_authority_context(cfg, hits)
    assert "Follow org policy." in text
    assert "Canonical" in text
    assert "- B" in text
    assert "Notes" in text
    assert "- A" in text
    assert "[UNASSIGNED]" in text
    assert "- C" in text


def test_apply_recall_authority_disabled_is_noop() -> None:
    r = RecallResult(hits=[], total_available=0, truncated=False)
    out = apply_recall_authority(r, RecallAuthorityConfig(enabled=False))
    assert out.authority_context is None


def test_apply_recall_authority_rules_only_when_no_tiers() -> None:
    cfg = RecallAuthorityConfig(enabled=True, rules_inline="Rules only.", tiers=[])
    r = RecallResult(hits=[MemoryHit(text="x", score=1.0)], total_available=1, truncated=False)
    out = apply_recall_authority(r, cfg)
    assert out.authority_context == "Rules only."


def test_validate_duplicate_tier_ids(tmp_path) -> None:
    cfg = AstrocyteConfig()
    cfg.recall_authority = RecallAuthorityConfig(
        enabled=True,
        tiers=[
            RecallAuthorityTierConfig(id="a", priority=1, label="A"),
            RecallAuthorityTierConfig(id="a", priority=2, label="B"),
        ],
    )
    with pytest.raises(ConfigError, match="duplicate"):
        validate_astrocyte_config(cfg)


def test_validate_empty_tier_id_when_tiers_present(tmp_path) -> None:
    cfg = AstrocyteConfig()
    cfg.recall_authority = RecallAuthorityConfig(
        enabled=True,
        tiers=[RecallAuthorityTierConfig(id="", priority=1, label="X")],
    )
    with pytest.raises(ConfigError, match="non-empty id"):
        validate_astrocyte_config(cfg)


def test_load_config_parses_recall_authority(tmp_path) -> None:
    p = tmp_path / "c.yaml"
    p.write_text(
        """
recall_authority:
  enabled: true
  apply_to_reflect: false
  rules_inline: "R"
  tier_by_bank:
    mem: t1
  tiers:
    - id: t1
      priority: 1
      label: "T1"
""",
        encoding="utf-8",
    )
    cfg = load_config(p)
    assert cfg.recall_authority.enabled is True
    assert cfg.recall_authority.apply_to_reflect is False
    assert cfg.recall_authority.rules_inline == "R"
    assert cfg.recall_authority.tier_by_bank == {"mem": "t1"}
    assert len(cfg.recall_authority.tiers) == 1
    assert cfg.recall_authority.tiers[0].id == "t1"
