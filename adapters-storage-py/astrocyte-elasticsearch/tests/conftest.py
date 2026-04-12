"""Skip integration tests when Elasticsearch is unreachable."""

from __future__ import annotations

import os

import pytest

ES_URL = os.environ.get("ASTROCYTE_ELASTICSEARCH_URL", "http://127.0.0.1:9200")


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "integration: requires Docker services")


@pytest.fixture(scope="session")
def elasticsearch_available() -> bool:
    try:
        import urllib.request

        urllib.request.urlopen(ES_URL, timeout=2.0).read()
        return True
    except Exception:
        return False


@pytest.fixture
def require_elasticsearch(elasticsearch_available: bool) -> None:
    if not elasticsearch_available:
        pytest.skip(f"Elasticsearch not reachable at {ES_URL}")
