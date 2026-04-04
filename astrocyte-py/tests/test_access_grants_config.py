"""access_grants / banks.*.access loading and Tier 1 retain allow/deny."""

from __future__ import annotations

from pathlib import Path

import pytest

from astrocyte._astrocyte import Astrocyte
from astrocyte.config import access_grants_for_astrocyte, load_config
from astrocyte.errors import AccessDenied
from astrocyte.pipeline.orchestrator import PipelineOrchestrator
from astrocyte.testing.in_memory import InMemoryVectorStore, MockLLMProvider
from astrocyte.types import AstrocyteContext


def _tier1_from_config(path: Path) -> Astrocyte:
    config = load_config(path)
    brain = Astrocyte(config)
    pipeline = PipelineOrchestrator(vector_store=InMemoryVectorStore(), llm_provider=MockLLMProvider())
    brain.set_pipeline(pipeline)
    if config.access_control.enabled:
        brain.set_access_grants(access_grants_for_astrocyte(config))
    return brain


class TestAccessGrantsYaml:
    def test_load_access_grants_and_banks_access(self, tmp_path: Path):
        cfg = tmp_path / "c.yaml"
        cfg.write_text(
            """
provider_tier: storage
vector_store: in_memory
llm_provider: mock
barriers:
  pii:
    mode: disabled
escalation:
  degraded_mode: error
access_control:
  enabled: true
  default_policy: deny
access_grants:
  - bank_id: top
    principal: agent:top
    permissions: [read, write]
banks:
  nested:
    access:
      - principal: agent:nested
        permissions: [read]
"""
        )
        config = load_config(cfg)
        grants = access_grants_for_astrocyte(config)
        principals = {(g.bank_id, g.principal) for g in grants}
        assert ("top", "agent:top") in principals
        assert ("nested", "agent:nested") in principals

    @pytest.mark.asyncio
    async def test_retain_allowed_for_configured_principal(self, tmp_path: Path):
        cfg = tmp_path / "c.yaml"
        cfg.write_text(
            """
provider_tier: storage
vector_store: in_memory
llm_provider: mock
barriers:
  pii:
    mode: disabled
escalation:
  degraded_mode: error
access_control:
  enabled: true
  default_policy: deny
access_grants:
  - bank_id: bank-1
    principal: agent:alice
    permissions: [read, write]
"""
        )
        brain = _tier1_from_config(cfg)
        ctx = AstrocyteContext(principal="agent:alice")
        result = await brain.retain("hello", bank_id="bank-1", context=ctx)
        assert result.stored is True

    @pytest.mark.asyncio
    async def test_retain_denied_for_other_principal(self, tmp_path: Path):
        cfg = tmp_path / "c.yaml"
        cfg.write_text(
            """
provider_tier: storage
vector_store: in_memory
llm_provider: mock
barriers:
  pii:
    mode: disabled
escalation:
  degraded_mode: error
access_control:
  enabled: true
  default_policy: deny
access_grants:
  - bank_id: bank-1
    principal: agent:alice
    permissions: [read, write]
"""
        )
        brain = _tier1_from_config(cfg)
        ctx = AstrocyteContext(principal="agent:bob")
        with pytest.raises(AccessDenied):
            await brain.retain("hello", bank_id="bank-1", context=ctx)
