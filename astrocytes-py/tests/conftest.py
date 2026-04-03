"""Shared test fixtures."""

from __future__ import annotations

from pathlib import Path

import pytest

from astrocytes.testing.in_memory import (
    InMemoryDocumentStore,
    InMemoryEngineProvider,
    InMemoryGraphStore,
    InMemoryVectorStore,
    MockLLMProvider,
)


@pytest.fixture
def vector_store() -> InMemoryVectorStore:
    return InMemoryVectorStore()


@pytest.fixture
def graph_store() -> InMemoryGraphStore:
    return InMemoryGraphStore()


@pytest.fixture
def document_store() -> InMemoryDocumentStore:
    return InMemoryDocumentStore()


@pytest.fixture
def engine_provider() -> InMemoryEngineProvider:
    return InMemoryEngineProvider()


@pytest.fixture
def mock_llm() -> MockLLMProvider:
    return MockLLMProvider()


@pytest.fixture
def sample_config_path(tmp_path: Path) -> Path:
    config = tmp_path / "astrocytes.yaml"
    config.write_text(
        """
profile: minimal
provider_tier: engine
provider: test
provider_config:
  endpoint: http://localhost:8080

homeostasis:
  rate_limits:
    retain_per_minute: 60
    recall_per_minute: 120

barriers:
  pii:
    mode: regex
    action: redact
  validation:
    max_content_length: 10000
    reject_empty_content: true

escalation:
  degraded_mode: empty_recall
"""
    )
    return config


@pytest.fixture
def support_config_path(tmp_path: Path) -> Path:
    config = tmp_path / "support.yaml"
    config.write_text(
        """
profile: support
provider_tier: engine
provider: test
"""
    )
    return config
