"""Astrocyte built-in intelligence pipeline (Tier 1).

Active when provider_tier = "storage". Steps aside for Tier 2 engine providers.
See docs/_design/built-in-pipeline.md.
"""

from astrocyte.pipeline.extraction import (
    BUILTIN_EXTRACTION_PROFILES,
    PreparedRetainInput,
    extraction_profile_for_source,
    merged_extraction_profiles,
    merged_user_and_builtin_profiles,
    prepare_retain_input,
    resolve_retain_chunking,
    resolve_retain_fact_type,
)

__all__ = [
    "BUILTIN_EXTRACTION_PROFILES",
    "PreparedRetainInput",
    "extraction_profile_for_source",
    "merged_extraction_profiles",
    "merged_user_and_builtin_profiles",
    "prepare_retain_input",
    "resolve_retain_chunking",
    "resolve_retain_fact_type",
]
