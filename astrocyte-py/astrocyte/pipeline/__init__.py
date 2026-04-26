"""Astrocyte built-in intelligence pipeline (Tier 1).

Active when provider_tier = "storage". Steps aside for Tier 2 engine providers.
See docs/_design/built-in-pipeline.md.
"""

from astrocyte.pipeline.extraction import (
    BUILTIN_EXTRACTION_PROFILES,
    ChunkingDecision,
    PreparedRetainInput,
    extraction_profile_for_source,
    merged_extraction_profiles,
    merged_user_and_builtin_profiles,
    prepare_retain_input,
    resolve_retain_chunking,
    resolve_retain_fact_type,
)
from astrocyte.pipeline.tasks import (
    ANALYZE_BENCHMARK_FAILURES,
    COMPILE_BANK,
    COMPILE_PERSONA_PAGE,
    INDEX_WIKI_PAGE_VECTOR,
    LINT_WIKI_PAGE,
    NORMALIZE_TEMPORAL_FACTS,
    PROJECT_ENTITY_EDGES,
    InMemoryTaskBackend,
    MemoryTask,
    MemoryTaskDispatcher,
    MemoryTaskWorker,
    TaskHandlerContext,
)

try:
    from astrocyte.pipeline.pgqueuer_tasks import PgQueuerMemoryTaskQueue
except ImportError:  # pragma: no cover - optional worker extra
    PgQueuerMemoryTaskQueue = None  # type: ignore[assignment]

__all__ = [
    "BUILTIN_EXTRACTION_PROFILES",
    "ChunkingDecision",
    "PreparedRetainInput",
    "ANALYZE_BENCHMARK_FAILURES",
    "COMPILE_BANK",
    "COMPILE_PERSONA_PAGE",
    "INDEX_WIKI_PAGE_VECTOR",
    "LINT_WIKI_PAGE",
    "NORMALIZE_TEMPORAL_FACTS",
    "PROJECT_ENTITY_EDGES",
    "InMemoryTaskBackend",
    "MemoryTask",
    "MemoryTaskDispatcher",
    "MemoryTaskWorker",
    "PgQueuerMemoryTaskQueue",
    "TaskHandlerContext",
    "extraction_profile_for_source",
    "merged_extraction_profiles",
    "merged_user_and_builtin_profiles",
    "prepare_retain_input",
    "resolve_retain_chunking",
    "resolve_retain_fact_type",
]
