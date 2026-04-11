"""Built-in entry points for Tier 1 providers (see `pyproject.toml`)."""

import pytest

from astrocyte._discovery import resolve_provider


def _editable_install_entry_points_available() -> bool:
    """Entry points from ``pyproject.toml`` exist only after ``pip install -e .`` (or a normal install)."""
    try:
        resolve_provider("in_memory", "vector_stores")
        return True
    except LookupError:
        return False


pytestmark = pytest.mark.skipif(
    not _editable_install_entry_points_available(),
    reason="Astrocyte entry points not registered (use `pip install -e ./astrocyte-py` from the repo root)",
)


def test_in_memory_vector_store_entry_point() -> None:
    cls = resolve_provider("in_memory", "vector_stores")
    assert cls.__name__ == "InMemoryVectorStore"


def test_mock_llm_entry_point() -> None:
    cls = resolve_provider("mock", "llm_providers")
    assert cls.__name__ == "MockLLMProvider"


def test_in_memory_graph_and_document_entry_points() -> None:
    g = resolve_provider("in_memory", "graph_stores")
    d = resolve_provider("in_memory", "document_stores")
    assert g.__name__ == "InMemoryGraphStore"
    assert d.__name__ == "InMemoryDocumentStore"
