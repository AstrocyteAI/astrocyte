"""Construct a Tier 1 Astrocyte with in-memory stores (reference deployment)."""

from __future__ import annotations

import os
from pathlib import Path

from astrocytes import Astrocyte
from astrocytes.config import AstrocyteConfig, load_config
from astrocytes.pipeline.orchestrator import PipelineOrchestrator
from astrocytes.testing.in_memory import InMemoryVectorStore, MockLLMProvider


def build_reference_astrocyte() -> Astrocyte:
    """Load optional YAML policy config; always wire Tier 1 in-memory pipeline."""
    path = os.environ.get("ASTROCYTES_CONFIG_PATH")
    if path and Path(path).is_file():
        config = load_config(path)
    else:
        config = AstrocyteConfig()

    config.provider_tier = "storage"
    if not path or not Path(path).is_file():
        config.barriers.pii.mode = "disabled"
        config.escalation.degraded_mode = "error"
        config.access_control.enabled = False

    brain = Astrocyte(config)
    vector_store = InMemoryVectorStore()
    llm = MockLLMProvider()
    pipeline = PipelineOrchestrator(vector_store=vector_store, llm_provider=llm)
    brain.set_pipeline(pipeline)
    return brain
