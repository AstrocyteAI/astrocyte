"""Integration test configuration for astrocyte-age.

Tests require a live PostgreSQL instance with Apache AGE installed.
Set ASTROCYTE_AGE_TEST_DSN to run them:

    ASTROCYTE_AGE_TEST_DSN=postgresql://user:pass@localhost:5434/astrocyte \
        pytest tests/ -v

The docker-compose.yml at the repo root spins up a compatible instance:

    docker compose -f astrocyte-services-py/docker-compose-age.yml up -d
    # wait for healthy, then:
    ASTROCYTE_AGE_TEST_DSN=postgresql://astrocyte:astrocyte@localhost:5434/astrocyte \
        pytest tests/ -v
"""

import os

import pytest


def pytest_collection_modifyitems(config, items):
    """Skip integration tests when ASTROCYTE_AGE_TEST_DSN is not set."""
    dsn = os.environ.get("ASTROCYTE_AGE_TEST_DSN")
    skip = pytest.mark.skip(reason="Set ASTROCYTE_AGE_TEST_DSN to run AGE integration tests")
    if not dsn:
        for item in items:
            item.add_marker(skip)


@pytest.fixture
def age_dsn() -> str:
    return os.environ["ASTROCYTE_AGE_TEST_DSN"]
