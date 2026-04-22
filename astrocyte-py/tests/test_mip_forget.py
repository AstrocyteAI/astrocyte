"""MIP forget vocabulary (Phase 4).

Verifies ForgetSpec authoring + enforcement:
- Loader: parses forget block, enforces P2 (version), validates enums,
  expands presets (gdpr / student / audit-strict).
- Router: ``resolve_forget_for_bank`` returns the right spec per bank;
  ``RoutingDecision.forget`` is propagated.
- _astrocyte.forget(): respects ``max_per_call``, emits audit log when
  ``audit`` is recommended/required, ``respect_legal_hold`` overrides
  ``compliance=True`` bypass.
"""

from __future__ import annotations

import logging
import textwrap
from pathlib import Path

import pytest

from astrocyte import Astrocyte
from astrocyte.config import AstrocyteConfig
from astrocyte.errors import ConfigError, LegalHoldActive, MipRoutingError
from astrocyte.mip.loader import _parse_mip_config, load_mip_config
from astrocyte.mip.presets import (
    FORGET_PRESETS,
    expand_forget_preset,
    is_known_forget_preset,
)
from astrocyte.mip.router import MipRouter
from astrocyte.mip.schema import (
    ActionSpec,
    BankDefinition,
    ForgetSpec,
    MatchBlock,
    MatchSpec,
    MipConfig,
    RoutingRule,
)
from astrocyte.testing.in_memory import InMemoryEngineProvider
from astrocyte.types import AstrocyteContext

# ---------------------------------------------------------------------------
# Schema + presets
# ---------------------------------------------------------------------------


class TestForgetPresets:
    def test_gdpr_preset_resolves_to_hard_audit_required_cascade(self):
        spec = expand_forget_preset(ForgetSpec(version=1, preset="gdpr"))
        assert spec.preset is None  # cleared post-expansion
        assert spec.mode == "hard"
        assert spec.audit == "required"
        assert spec.cascade is True
        assert spec.respect_legal_hold is True

    def test_student_preset_resolves_to_soft_min_age_seven(self):
        spec = expand_forget_preset(ForgetSpec(version=1, preset="student"))
        assert spec.mode == "soft"
        assert spec.min_age_days == 7
        assert spec.respect_legal_hold is True

    def test_explicit_field_overrides_preset_default(self):
        spec = expand_forget_preset(
            ForgetSpec(version=1, preset="gdpr", min_age_days=30)
        )
        assert spec.mode == "hard"  # from preset
        assert spec.min_age_days == 30  # explicit override wins

    def test_no_preset_returns_spec_unchanged(self):
        spec = ForgetSpec(version=1, mode="soft")
        assert expand_forget_preset(spec) is spec

    def test_known_preset_set(self):
        assert {"gdpr", "student", "audit-strict"} <= set(FORGET_PRESETS.keys())
        assert is_known_forget_preset("gdpr")
        assert not is_known_forget_preset("not-a-preset")


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------


class TestForgetLoader:
    def test_parses_minimal_forget_block(self):
        cfg = _parse_mip_config({
            "rules": [{
                "name": "r",
                "priority": 10,
                "match": {"content_type": "pii"},
                "action": {"bank": "vault", "forget": {"version": 1, "mode": "soft"}},
            }],
        })
        rule = cfg.rules[0]
        assert rule.action.forget is not None
        assert rule.action.forget.version == 1
        assert rule.action.forget.mode == "soft"

    def test_missing_version_errors(self):
        with pytest.raises(ConfigError, match="forget.version is required"):
            _parse_mip_config({
                "rules": [{
                    "name": "r", "priority": 10,
                    "match": {"content_type": "pii"},
                    "action": {"bank": "vault", "forget": {"mode": "soft"}},
                }],
            })

    def test_invalid_mode_errors(self):
        with pytest.raises(ConfigError, match="forget.mode"):
            _parse_mip_config({
                "rules": [{
                    "name": "r", "priority": 10,
                    "match": {"content_type": "pii"},
                    "action": {"bank": "vault", "forget": {"version": 1, "mode": "purge"}},
                }],
            })

    def test_invalid_audit_errors(self):
        with pytest.raises(ConfigError, match="forget.audit"):
            _parse_mip_config({
                "rules": [{
                    "name": "r", "priority": 10,
                    "match": {"content_type": "pii"},
                    "action": {"bank": "vault", "forget": {"version": 1, "audit": "loud"}},
                }],
            })

    def test_negative_min_age_errors(self):
        with pytest.raises(ConfigError, match="min_age_days"):
            _parse_mip_config({
                "rules": [{
                    "name": "r", "priority": 10,
                    "match": {"content_type": "pii"},
                    "action": {"bank": "vault", "forget": {"version": 1, "min_age_days": -1}},
                }],
            })

    def test_zero_max_per_call_errors(self):
        with pytest.raises(ConfigError, match="max_per_call"):
            _parse_mip_config({
                "rules": [{
                    "name": "r", "priority": 10,
                    "match": {"content_type": "pii"},
                    "action": {"bank": "vault", "forget": {"version": 1, "max_per_call": 0}},
                }],
            })

    def test_unknown_preset_errors_with_known_list(self):
        with pytest.raises(ConfigError, match="unknown forget preset"):
            _parse_mip_config({
                "rules": [{
                    "name": "r", "priority": 10,
                    "match": {"content_type": "pii"},
                    "action": {"bank": "vault", "forget": {"version": 1, "preset": "nope"}},
                }],
            })

    def test_hard_mode_requires_audit_required(self):
        # Discipline rule: hard delete without required audit is rejected at load
        with pytest.raises(ConfigError, match="hard.*audit.*required"):
            _parse_mip_config({
                "rules": [{
                    "name": "r", "priority": 10,
                    "match": {"content_type": "pii"},
                    "action": {"bank": "vault", "forget": {"version": 1, "mode": "hard"}},
                }],
            })

    def test_gdpr_preset_satisfies_hard_audit_discipline(self):
        # gdpr preset is mode=hard + audit=required, so it must load cleanly.
        cfg = _parse_mip_config({
            "rules": [{
                "name": "r", "priority": 10,
                "match": {"content_type": "pii"},
                "action": {"bank": "vault", "forget": {"version": 1, "preset": "gdpr"}},
            }],
        })
        assert cfg.rules[0].action.forget.mode == "hard"
        assert cfg.rules[0].action.forget.audit == "required"

    def test_loader_via_yaml_file(self, tmp_path: Path):
        path = tmp_path / "mip.yaml"
        path.write_text(textwrap.dedent("""\
            version: "1.0"
            rules:
              - name: gdpr-erasure
                priority: 1
                match: { content_type: pii }
                action:
                  bank: vault
                  forget:
                    version: 1
                    preset: gdpr
        """))
        cfg = load_mip_config(path)
        spec = cfg.rules[0].action.forget
        assert spec.mode == "hard"
        assert spec.cascade is True


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


def _router_with_forget_rule(spec: ForgetSpec, bank: str = "vault") -> MipRouter:
    rule = RoutingRule(
        name="r",
        priority=10,
        match=MatchBlock(all_conditions=[MatchSpec(field="content_type", operator="eq", value="pii")]),
        action=ActionSpec(bank=bank, forget=spec),
    )
    return MipRouter(MipConfig(rules=[rule], banks=[BankDefinition(id=bank)]))


class TestResolveForgetForBank:
    def test_returns_spec_for_matching_bank(self):
        spec = ForgetSpec(version=1, mode="soft")
        router = _router_with_forget_rule(spec, bank="vault")
        assert router.resolve_forget_for_bank("vault") is spec

    def test_template_bank_matches_concrete_id(self):
        spec = ForgetSpec(version=1, mode="soft")
        router = _router_with_forget_rule(spec, bank="student-{id}")
        assert router.resolve_forget_for_bank("student-42") is spec

    def test_returns_none_when_no_rule_targets_bank(self):
        spec = ForgetSpec(version=1, mode="soft")
        router = _router_with_forget_rule(spec, bank="vault")
        assert router.resolve_forget_for_bank("other-bank") is None

    def test_returns_none_when_rule_has_no_forget(self):
        rule = RoutingRule(
            name="r", priority=10,
            match=MatchBlock(all_conditions=[MatchSpec(field="content_type", operator="eq", value="x")]),
            action=ActionSpec(bank="vault"),  # no forget
        )
        router = MipRouter(MipConfig(rules=[rule]))
        assert router.resolve_forget_for_bank("vault") is None


# ---------------------------------------------------------------------------
# _astrocyte.forget() honors MIP forget spec
# ---------------------------------------------------------------------------


def _make_brain_with_router(tmp_path: Path, mip_yaml: str) -> Astrocyte:
    path = tmp_path / "mip.yaml"
    path.write_text(mip_yaml)
    config = AstrocyteConfig()
    config.mip_config_path = str(path)
    config.barriers.pii.mode = "disabled"
    config.barriers.validation.allowed_content_types = ["text", "pii"]
    brain = Astrocyte(config)
    brain.set_engine_provider(InMemoryEngineProvider())
    return brain


@pytest.mark.asyncio
class TestForgetMaxPerCall:
    async def test_exceeding_max_per_call_raises(self, tmp_path: Path):
        yaml = textwrap.dedent("""\
            version: "1.0"
            rules:
              - name: capped
                priority: 10
                match: { content_type: pii }
                action:
                  bank: vault
                  forget:
                    version: 1
                    mode: soft
                    max_per_call: 2
        """)
        brain = _make_brain_with_router(tmp_path, yaml)
        with pytest.raises(MipRoutingError, match="max_per_call"):
            await brain.forget("vault", memory_ids=["a", "b", "c"])

    async def test_within_max_per_call_succeeds(self, tmp_path: Path):
        yaml = textwrap.dedent("""\
            version: "1.0"
            rules:
              - name: capped
                priority: 10
                match: { content_type: pii }
                action:
                  bank: vault
                  forget:
                    version: 1
                    mode: soft
                    max_per_call: 5
        """)
        brain = _make_brain_with_router(tmp_path, yaml)
        # No raise — count under the cap; deletion of nonexistent IDs is a no-op
        result = await brain.forget("vault", memory_ids=["a", "b"])
        assert result.deleted_count == 0


@pytest.mark.asyncio
class TestForgetAuditLogging:
    async def test_audit_required_emits_warning_log(self, tmp_path: Path, caplog):
        yaml = textwrap.dedent("""\
            version: "1.0"
            rules:
              - name: gdpr
                priority: 10
                match: { content_type: pii }
                action:
                  bank: vault
                  forget:
                    version: 1
                    preset: gdpr
        """)
        brain = _make_brain_with_router(tmp_path, yaml)
        with caplog.at_level(logging.WARNING):
            await brain.forget(
                "vault",
                memory_ids=["a"],
                compliance=True,
                reason="user request",
                context=AstrocyteContext(principal="user:test"),
            )
        # Audit message includes mode and bank
        assert any(
            "astrocyte.mip.forget.audit" in (r.message + str(getattr(r, "data", "")))
            or "mip.forget.audit" in r.getMessage()
            for r in caplog.records
        )

    async def test_no_audit_when_no_router_rule(self, tmp_path: Path, caplog):
        yaml = textwrap.dedent("""\
            version: "1.0"
            rules:
              - name: other-bank
                priority: 10
                match: { content_type: pii }
                action:
                  bank: other
                  forget: { version: 1, mode: soft }
        """)
        brain = _make_brain_with_router(tmp_path, yaml)
        with caplog.at_level(logging.WARNING):
            await brain.forget("vault", memory_ids=["a"])
        assert not any("mip.forget.audit" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
class TestForgetRespectLegalHold:
    async def test_legal_hold_blocks_compliance_forget_when_mip_says_respect(
        self, tmp_path: Path,
    ):
        """A rule with respect_legal_hold=True overrides compliance=True bypass."""
        yaml = textwrap.dedent("""\
            version: "1.0"
            rules:
              - name: gdpr
                priority: 10
                match: { content_type: pii }
                action:
                  bank: vault
                  forget:
                    version: 1
                    preset: gdpr
        """)
        brain = _make_brain_with_router(tmp_path, yaml)
        brain.set_legal_hold("vault", "h1", "litigation hold")
        with pytest.raises(LegalHoldActive):
            await brain.forget(
                "vault",
                memory_ids=["a"],
                compliance=True,
                reason="rtbf",
                context=AstrocyteContext(principal="user:test"),
            )

    async def test_legal_hold_bypass_when_respect_false(self, tmp_path: Path):
        """A rule with respect_legal_hold=false lets compliance bypass through."""
        yaml = textwrap.dedent("""\
            version: "1.0"
            rules:
              - name: rtbf-overrides-hold
                priority: 10
                match: { content_type: pii }
                action:
                  bank: vault
                  forget:
                    version: 1
                    mode: hard
                    audit: required
                    respect_legal_hold: false
        """)
        brain = _make_brain_with_router(tmp_path, yaml)
        brain.set_legal_hold("vault", "h1", "hold")
        # compliance=True + respect_legal_hold=false → bypass succeeds
        result = await brain.forget(
            "vault",
            memory_ids=["a"],
            compliance=True,
            reason="rtbf",
            context=AstrocyteContext(principal="user:test"),
        )
        assert result.deleted_count == 0  # nonexistent id, but no raise


@pytest.mark.asyncio
class TestForgetMinAgeDays:
    _YAML = textwrap.dedent("""\
        version: "1.0"
        rules:
          - name: aged
            priority: 10
            match: { content_type: pii }
            action:
              bank: vault
              forget:
                version: 1
                mode: soft
                min_age_days: 7
    """)

    async def test_record_younger_than_min_age_is_refused(self, tmp_path: Path):
        brain = _make_brain_with_router(tmp_path, self._YAML)
        from astrocyte.types import RetainRequest

        # InMemory engine stamps `_created_at` at retain time → just-stored
        # records are 0 days old and must be refused.
        result = await brain._engine_provider.retain(
            RetainRequest(bank_id="vault", content="ssn 1", content_type="pii"),
        )
        with pytest.raises(MipRoutingError, match="min_age_days"):
            await brain.forget("vault", memory_ids=[result.memory_id])

    async def test_record_older_than_min_age_proceeds(self, tmp_path: Path):
        from datetime import datetime, timedelta, timezone

        from astrocyte.types import MemoryHit

        brain = _make_brain_with_router(tmp_path, self._YAML)
        # Inject a record with a backdated `_created_at` directly into the
        # engine, bypassing retain() so we control the stamp.
        old_iso = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        brain._engine_provider._memories["vault"] = [
            MemoryHit(
                text="old", score=1.0, fact_type="world",
                metadata={"_created_at": old_iso},
                memory_id="old-id", bank_id="vault",
            )
        ]
        # No raise; deletion succeeds because the record is older than 7 days.
        result = await brain.forget("vault", memory_ids=["old-id"])
        assert result.deleted_count == 1

    async def test_record_missing_created_at_is_skipped_with_warning(
        self, tmp_path: Path, caplog,
    ):
        from astrocyte.types import MemoryHit

        brain = _make_brain_with_router(tmp_path, self._YAML)
        brain._engine_provider._memories["vault"] = [
            MemoryHit(
                text="undated", score=1.0, fact_type="world",
                metadata={},  # no _created_at
                memory_id="undated-id", bank_id="vault",
            )
        ]
        with caplog.at_level(logging.WARNING, logger="astrocyte.mip"):
            result = await brain.forget("vault", memory_ids=["undated-id"])
        assert result.deleted_count == 1  # degraded enforcement → forget proceeds
        assert any("min_age_days enforcement degraded" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
class TestForgetCascadeFlag:
    """``cascade`` flag flows from yaml → ForgetSpec → audit log.

    Cascade itself is an authoring signal for derived-data deletion (chunks,
    embeddings, graph links). Today the runtime only surfaces it in the audit
    log so operators can verify policy. These tests pin that contract.
    """

    async def test_cascade_true_surfaces_in_audit_log(self, tmp_path: Path, caplog):
        yaml = textwrap.dedent("""\
            version: "1.0"
            rules:
              - name: cascade-on
                priority: 10
                match: { content_type: pii }
                action:
                  bank: vault
                  forget:
                    version: 1
                    mode: hard
                    audit: required
                    cascade: true
        """)
        brain = _make_brain_with_router(tmp_path, yaml)
        with caplog.at_level(logging.WARNING):
            await brain.forget(
                "vault", memory_ids=["a"],
                compliance=True, reason="rtbf",
                context=AstrocyteContext(principal="user:test"),
            )
        audit = next(r for r in caplog.records if "mip.forget.audit" in r.getMessage())
        assert '"cascade": true' in audit.getMessage()

    async def test_cascade_false_surfaces_in_audit_log(self, tmp_path: Path, caplog):
        yaml = textwrap.dedent("""\
            version: "1.0"
            rules:
              - name: cascade-off
                priority: 10
                match: { content_type: pii }
                action:
                  bank: vault
                  forget:
                    version: 1
                    mode: hard
                    audit: required
                    cascade: false
        """)
        brain = _make_brain_with_router(tmp_path, yaml)
        with caplog.at_level(logging.WARNING):
            await brain.forget(
                "vault", memory_ids=["a"],
                compliance=True, reason="rtbf",
                context=AstrocyteContext(principal="user:test"),
            )
        audit = next(r for r in caplog.records if "mip.forget.audit" in r.getMessage())
        assert '"cascade": false' in audit.getMessage()

    async def test_cascade_omitted_defaults_true_in_audit(self, tmp_path: Path, caplog):
        # gdpr preset implies cascade=true; no explicit override here.
        yaml = textwrap.dedent("""\
            version: "1.0"
            rules:
              - name: gdpr
                priority: 10
                match: { content_type: pii }
                action:
                  bank: vault
                  forget: { version: 1, preset: gdpr }
        """)
        brain = _make_brain_with_router(tmp_path, yaml)
        with caplog.at_level(logging.WARNING):
            await brain.forget(
                "vault", memory_ids=["a"],
                compliance=True, reason="rtbf",
                context=AstrocyteContext(principal="user:test"),
            )
        audit = next(r for r in caplog.records if "mip.forget.audit" in r.getMessage())
        assert '"cascade": true' in audit.getMessage()

    async def test_cascade_loader_round_trip(self):
        """Loader preserves cascade=False (does not collapse to None default)."""
        config = _parse_mip_config({
            "version": "1.0",
            "rules": [{
                "name": "r",
                "priority": 10,
                "match": {"content_type": "pii"},
                "action": {
                    "bank": "vault",
                    "forget": {"version": 1, "mode": "soft", "cascade": False},
                },
            }],
        })
        spec = config.rules[0].action.forget
        assert spec.cascade is False  # explicit False, not None


@pytest.mark.asyncio
class TestForgetSoftDelete:
    """``forget.mode=soft`` marks records with ``_deleted: true`` instead of
    physically removing them, and recall must hide soft-deleted records."""

    _SOFT_YAML = textwrap.dedent("""\
        version: "1.0"
        rules:
          - name: soft-rule
            priority: 10
            match: { content_type: pii }
            action:
              bank: vault
              forget: { version: 1, mode: soft }
    """)

    _HARD_YAML = textwrap.dedent("""\
        version: "1.0"
        rules:
          - name: hard-rule
            priority: 10
            match: { content_type: pii }
            action:
              bank: vault
              forget: { version: 1, mode: hard, audit: required }
    """)

    async def test_soft_delete_marks_metadata_and_hides_from_recall(self, tmp_path: Path):
        from astrocyte.types import RecallRequest, RetainRequest

        brain = _make_brain_with_router(tmp_path, self._SOFT_YAML)
        r1 = await brain._engine_provider.retain(
            RetainRequest(bank_id="vault", content="kept", content_type="pii"),
        )
        r2 = await brain._engine_provider.retain(
            RetainRequest(bank_id="vault", content="forgotten", content_type="pii"),
        )

        result = await brain.forget("vault", memory_ids=[r2.memory_id])
        assert result.deleted_count == 1

        # Record still physically present, but flagged
        all_mem = brain._engine_provider._memories["vault"]
        assert len(all_mem) == 2
        forgotten = next(m for m in all_mem if m.memory_id == r2.memory_id)
        assert forgotten.metadata["_deleted"] is True

        # Recall hides the soft-deleted record
        recall = await brain._engine_provider.recall(
            RecallRequest(query="kept forgotten", bank_id="vault", max_results=10),
        )
        ids = [h.memory_id for h in recall.hits]
        assert r1.memory_id in ids
        assert r2.memory_id not in ids

    async def test_hard_mode_still_physically_deletes(self, tmp_path: Path):
        from astrocyte.types import RetainRequest

        brain = _make_brain_with_router(tmp_path, self._HARD_YAML)
        r1 = await brain._engine_provider.retain(
            RetainRequest(bank_id="vault", content="bye", content_type="pii"),
        )
        result = await brain.forget(
            "vault", memory_ids=[r1.memory_id],
            compliance=True, reason="rtbf",
            context=AstrocyteContext(principal="user:test"),
        )
        assert result.deleted_count == 1
        assert brain._engine_provider._memories["vault"] == []

    async def test_soft_delete_is_idempotent(self, tmp_path: Path):
        from astrocyte.types import RetainRequest

        brain = _make_brain_with_router(tmp_path, self._SOFT_YAML)
        r1 = await brain._engine_provider.retain(
            RetainRequest(bank_id="vault", content="x", content_type="pii"),
        )
        first = await brain.forget("vault", memory_ids=[r1.memory_id])
        second = await brain.forget("vault", memory_ids=[r1.memory_id])
        assert first.deleted_count == 1
        assert second.deleted_count == 0  # already soft-deleted, no-op


@pytest.mark.asyncio
class TestForgetNoRouterPreservesBehavior:
    async def test_forget_unchanged_when_no_mip_router(self, tmp_path: Path):
        """Without mip_config_path, forget behaves exactly as before Phase 4."""
        config = AstrocyteConfig()
        config.barriers.pii.mode = "disabled"
        brain = Astrocyte(config)
        brain.set_engine_provider(InMemoryEngineProvider())
        # No router → no MIP enforcement → no MipRoutingError even for huge id list
        result = await brain.forget("vault", memory_ids=["x"] * 1000)
        assert result.deleted_count == 0
