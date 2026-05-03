from __future__ import annotations

from astrocyte.pipeline.preset_routing import route_recall_preset


def test_simple_lookup_routes_to_fast_recall() -> None:
    route = route_recall_preset("What is Alice's favorite coffee shop?")

    assert route.preset == "fast-recall"
    assert route.budget == "low"


def test_temporal_query_routes_to_hindsight_parity() -> None:
    route = route_recall_preset("When did Alice move to Boston?")

    assert route.preset == "hindsight-parity"
    assert route.budget == "mid"


def test_exploratory_query_routes_to_quality_max() -> None:
    route = route_recall_preset("Describe Alice's long-term working style")

    assert route.preset == "quality-max"
    assert route.budget == "high"
