"""Federated / proxy recall helpers (M4.1)."""

from astrocyte.recall.merge_result import merge_external_into_recall_result
from astrocyte.recall.oauth import clear_oauth2_token_cache_for_tests
from astrocyte.recall.proxy import (
    PLACE_BANK,
    PLACE_QUERY,
    build_proxy_headers,
    fetch_proxy_recall_hits,
    gather_proxy_hits_for_bank,
    merge_manual_and_proxy_hits,
)

__all__ = [
    "PLACE_BANK",
    "PLACE_QUERY",
    "build_proxy_headers",
    "clear_oauth2_token_cache_for_tests",
    "fetch_proxy_recall_hits",
    "gather_proxy_hits_for_bank",
    "merge_manual_and_proxy_hits",
    "merge_external_into_recall_result",
]
