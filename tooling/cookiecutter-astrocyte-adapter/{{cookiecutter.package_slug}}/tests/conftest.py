"""Skip {{ cookiecutter.backend_name }} tests when the backing service is unreachable.

The convention across all Astrocyte adapters: tests use a
``require_<backend>`` fixture that pytest-skips when the service can't
be contacted. Contributors running unit tests on laptops without Docker
get clean skips; CI runs the integration matrix with a live container.
"""

from __future__ import annotations

import os

import pytest

{{ cookiecutter.backend_label|upper }}_URL = os.environ.get(
    "ASTROCYTE_{{ cookiecutter.backend_label|upper }}_URL",
    "http://127.0.0.1:9090",  # TODO: replace with your backend's default port
)


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "integration: requires Docker services (e.g. {{ cookiecutter.backend_name }})",
    )


@pytest.fixture(scope="session")
def {{ cookiecutter.backend_label.replace('-', '_') }}_available() -> bool:
    """True iff {{ cookiecutter.backend_name }} is reachable. Cached per session."""
    try:
        # TODO: replace with your backend's reachability probe.
        # Example for an HTTP backend:
        # import httpx
        # httpx.get(f"{ {{ cookiecutter.backend_label|upper }}_URL}/health", timeout=2.0).raise_for_status()
        return False  # default: not reachable until probe is wired
    except Exception:
        return False


@pytest.fixture
def require_{{ cookiecutter.backend_label.replace('-', '_') }}({{ cookiecutter.backend_label.replace('-', '_') }}_available: bool) -> None:
    if not {{ cookiecutter.backend_label.replace('-', '_') }}_available:
        pytest.skip(f"{{ cookiecutter.backend_name }} not reachable at { {{ cookiecutter.backend_label|upper }}_URL}")
