"""Stable re-exports from astrocyte package (M3)."""

from __future__ import annotations

import astrocyte


def test_top_level_extraction_symbols():
    assert astrocyte.prepare_retain_input is not None
    assert astrocyte.merged_extraction_profiles is not None
    assert astrocyte.extraction_profile_for_source is not None
    assert astrocyte.PreparedRetainInput is not None
    assert "prepare_retain_input" in astrocyte.__all__


def test_pipeline_submodule_reexports():
    import astrocyte.pipeline as p

    assert p.prepare_retain_input is astrocyte.prepare_retain_input
    assert p.merged_extraction_profiles is astrocyte.merged_extraction_profiles
