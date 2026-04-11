"""Skip adapter tests when the backing service is unreachable (local dev without Docker)."""

from __future__ import annotations

import os

import pytest

QDRANT_URL = os.environ.get("ASTROCYTE_QDRANT_URL", "http://127.0.0.1:6333")


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "integration: requires Docker services (Qdrant, Neo4j, ES)")


@pytest.fixture(scope="session")
def qdrant_available() -> bool:
    try:
        from qdrant_client import QdrantClient

        c = QdrantClient(url=QDRANT_URL, timeout=2.0)
        c.get_collections()
        return True
    except Exception:
        return False


@pytest.fixture
def require_qdrant(qdrant_available: bool) -> None:
    if not qdrant_available:
        pytest.skip(f"Qdrant not reachable at {QDRANT_URL}")
