{% set class_suffix = {'vector_stores': 'VectorStore', 'graph_stores': 'GraphStore', 'document_stores': 'DocumentStore', 'pageindex_stores': 'PageIndexStore', 'wiki_stores': 'WikiStore', 'mental_model_stores': 'MentalModelStore', 'source_stores': 'SourceStore', 'llm_providers': 'LLMProvider'}[cookiecutter.spi_family] -%}
"""Smoke + Protocol-conformance tests for {{ cookiecutter.package_slug }}.

Two layers:

1. ``test_implements_protocol`` — runtime Protocol check. Passes once the
   adapter implements every method on ``astrocyte.provider.{{ class_suffix }}``.

2. ``test_health_roundtrip`` — actually talks to the backend (skipped
   when the service is unreachable; see ``conftest.py``).

For full SPI conformance, parametrize this file's ``store`` fixture
through the canonical contract test in
``astrocyte-py/tests/test_spi_{{ cookiecutter.spi_family[:-1] }}_contract.py`` —
see ``docs/_design/adapter-packages.md`` "Conformance".
"""

from __future__ import annotations

import pytest

from astrocyte.provider import {{ class_suffix }}
from {{ cookiecutter.module_slug }} import {{ cookiecutter.backend_name }}{{ class_suffix }}


def test_implements_protocol():
    """Adapter class must satisfy the SPI Protocol at runtime."""
    assert isinstance({{ cookiecutter.backend_name }}{{ class_suffix }}.__mro__, tuple)
    # Protocol's runtime_checkable check — adapter must expose every method
    # the {{ class_suffix }} Protocol requires.
    assert issubclass({{ cookiecutter.backend_name }}{{ class_suffix }}, {{ class_suffix }})


@pytest.mark.integration
async def test_health_roundtrip(require_{{ cookiecutter.backend_label.replace('-', '_') }}):
    """Round-trip the health endpoint against a live backend."""
    import os

    store = {{ cookiecutter.backend_name }}{{ class_suffix }}(
        url=os.environ.get("ASTROCYTE_{{ cookiecutter.backend_label|upper }}_URL", "http://127.0.0.1:9090"),
    )
    health = await store.health()
    assert health.healthy
