"""M2 config schema (v0.5.0 with M1): sources, agents, deployment, extraction_profiles, validation."""

from __future__ import annotations

from pathlib import Path

import pytest

from astrocyte.config import (
    AstrocyteConfig,
    access_grants_for_astrocyte,
    load_config,
    validate_astrocyte_config,
)
from astrocyte.errors import ConfigError


def _write(tmp_path: Path, text: str) -> Path:
    p = tmp_path / "c.yaml"
    p.write_text(text)
    return p


class TestM2Parsing:
    def test_no_new_sections_matches_defaults(self, sample_config_path: Path):
        config = load_config(sample_config_path)
        assert config.sources is None
        assert config.agents is None
        assert config.deployment is None
        assert config.extraction_profiles is None
        assert config.identity.obo_enabled is False
        assert config.identity.resolver is None

    def test_full_m2_roundtrip(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("WEBHOOK_SECRET", "s3cr3t")
        p = _write(
            tmp_path,
            """
provider_tier: engine
provider: test
extraction_profiles:
  conversation:
    chunking_strategy: dialogue
    entity_extraction: llm
sources:
  tavus:
    type: webhook
    extraction_profile: conversation
    target_bank_template: "user-{principal}"
    auth:
      type: hmac
      secret: ${WEBHOOK_SECRET}
agents:
  support-bot:
    principal: agent:support-bot
    banks: [shared-support, team_a]
deployment:
  mode: library
  port: 8420
identity:
  resolver: convention
  obo_enabled: false
""",
        )
        config = load_config(p)
        assert config.extraction_profiles is not None
        assert config.extraction_profiles["conversation"].chunking_strategy == "dialogue"
        assert config.sources is not None
        assert config.sources["tavus"].type == "webhook"
        assert config.sources["tavus"].auth is not None
        assert config.sources["tavus"].auth["secret"] == "s3cr3t"
        assert config.agents is not None
        assert config.agents["support-bot"].principal == "agent:support-bot"
        assert config.agents["support-bot"].banks == ["shared-support", "team_a"]
        assert config.deployment is not None
        assert config.deployment.mode == "library"
        assert config.deployment.port == 8420
        assert config.identity.resolver == "convention"
        assert config.identity.obo_enabled is False

    def test_allowed_banks_alias(self, tmp_path: Path):
        p = _write(
            tmp_path,
            """
provider: test
agents:
  a1:
    allowed_banks: [b1, b2]
    permissions: [read]
""",
        )
        config = load_config(p)
        assert config.agents is not None
        assert config.agents["a1"].banks == ["b1", "b2"]

    def test_deployment_with_tls(self, tmp_path: Path):
        p = _write(
            tmp_path,
            """
provider: test
deployment:
  mode: standalone
  host: 0.0.0.0
  tls:
    cert_path: /tmp/c.pem
    key_path: /tmp/k.pem
""",
        )
        config = load_config(p)
        assert config.deployment is not None
        assert config.deployment.mode == "standalone"
        assert config.deployment.tls is not None
        assert config.deployment.tls.cert_path == "/tmp/c.pem"


class TestM2Validation:
    def test_source_requires_type(self, tmp_path: Path):
        p = _write(
            tmp_path,
            """
provider: test
sources:
  bad: {}
""",
        )
        with pytest.raises(ConfigError, match="sources.bad: type is required"):
            load_config(p)

    def test_extraction_profile_must_exist(self, tmp_path: Path):
        p = _write(
            tmp_path,
            """
provider: test
extraction_profiles: {}
sources:
  s1:
    type: webhook
    extraction_profile: missing
""",
        )
        with pytest.raises(ConfigError, match="extraction_profile 'missing'"):
            load_config(p)

    def test_agent_bank_must_exist_when_banks_declared(self, tmp_path: Path):
        p = _write(
            tmp_path,
            """
provider: test
banks:
  alpha: {}
agents:
  bot:
    banks: [alpha, ghost]
""",
        )
        with pytest.raises(ConfigError, match="ghost"):
            load_config(p)

    def test_wildcard_requires_declared_banks(self, tmp_path: Path):
        p = _write(
            tmp_path,
            """
provider: test
agents:
  bot:
    banks: ["shared-*"]
""",
        )
        with pytest.raises(ConfigError, match="wildcards"):
            load_config(p)

    def test_wildcard_expands_against_banks(self, tmp_path: Path):
        p = _write(
            tmp_path,
            """
provider: test
banks:
  shared-a: {}
  shared-b: {}
  other: {}
agents:
  bot:
    banks: ["shared-*"]
""",
        )
        config = load_config(p)
        grants = access_grants_for_astrocyte(config)
        pairs = {(g.bank_id, g.principal) for g in grants}
        assert ("shared-a", "agent:bot") in pairs
        assert ("shared-b", "agent:bot") in pairs
        assert ("other", "agent:bot") not in pairs

    def test_identity_resolver_invalid(self, tmp_path: Path):
        p = _write(
            tmp_path,
            """
provider: test
identity:
  resolver: other
""",
        )
        with pytest.raises(ConfigError, match="identity.resolver"):
            load_config(p)

    def test_validate_astrocyte_config_manual(self):
        c = AstrocyteConfig()
        validate_astrocyte_config(c)  # no-op


class TestM2AccessGrants:
    def test_agent_grants_merged_and_deduped(self, tmp_path: Path):
        p = _write(
            tmp_path,
            """
provider: test
banks:
  b1: {}
access_grants:
  - bank_id: b1
    principal: agent:x
    permissions: [read, write]
agents:
  x:
    banks: [b1]
    permissions: [read, write]
""",
        )
        config = load_config(p)
        grants = access_grants_for_astrocyte(config)
        assert len([g for g in grants if g.bank_id == "b1" and g.principal == "agent:x"]) == 1
