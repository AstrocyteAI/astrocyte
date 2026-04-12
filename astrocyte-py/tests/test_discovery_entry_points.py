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


def test_kafka_ingest_stream_driver_entry_point() -> None:
    try:
        cls = resolve_provider("kafka", "ingest_stream_drivers")
    except LookupError:
        pytest.skip("astrocyte-ingestion-kafka not installed (no kafka stream driver entry point)")
    assert cls.__name__ == "KafkaStreamIngestSource"


def test_redis_ingest_stream_driver_entry_point() -> None:
    try:
        cls = resolve_provider("redis", "ingest_stream_drivers")
    except LookupError:
        pytest.skip("astrocyte-ingestion-redis not installed (no redis stream driver entry point)")
    assert cls.__name__ == "RedisStreamIngestSource"


def test_github_ingest_poll_driver_entry_point() -> None:
    try:
        cls = resolve_provider("github", "ingest_poll_drivers")
    except LookupError:
        pytest.skip("astrocyte-ingestion-github not installed (no github poll driver entry point)")
    assert cls.__name__ == "GithubPollIngestSource"
