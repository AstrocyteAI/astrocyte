"""Skip integration tests when Neo4j is unreachable."""

from __future__ import annotations

import os

import pytest

NEO4J_URI = os.environ.get("ASTROCYTE_NEO4J_URI", "bolt://127.0.0.1:7687")
NEO4J_USER = os.environ.get("ASTROCYTE_NEO4J_USER", "neo4j")
NEO4J_PASSWORD = os.environ.get("ASTROCYTE_NEO4J_PASSWORD", "testpass")


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "integration: requires Docker services")


@pytest.fixture(scope="session")
def neo4j_available() -> bool:
    try:
        from neo4j import GraphDatabase

        d = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))
        d.verify_connectivity()
        d.close()
        return True
    except Exception:
        return False


@pytest.fixture
def require_neo4j(neo4j_available: bool) -> None:
    if not neo4j_available:
        pytest.skip(f"Neo4j not reachable at {NEO4J_URI}")
