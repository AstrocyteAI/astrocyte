"""Stable re-exports from astrocyte package (M3)."""

from __future__ import annotations

import astrocyte
from astrocyte import (
    PreparedRetainInput,
    extraction_profile_for_source,
    merged_extraction_profiles,
    prepare_retain_input,
)


def test_top_level_extraction_symbols():
    assert prepare_retain_input is not None
    assert merged_extraction_profiles is not None
    assert extraction_profile_for_source is not None
    assert PreparedRetainInput is not None
    assert "prepare_retain_input" in astrocyte.__all__


def test_pipeline_submodule_reexports():
    import astrocyte.pipeline as p

    assert p.prepare_retain_input is prepare_retain_input
    assert p.merged_extraction_profiles is merged_extraction_profiles
