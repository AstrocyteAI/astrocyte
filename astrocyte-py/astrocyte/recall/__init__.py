"""Federated / proxy recall helpers (M4.1)."""

from astrocyte.recall.merge_result import merge_external_into_recall_result
from astrocyte.recall.oauth import (
    clear_oauth2_token_cache_for_tests,
    exchange_oauth2_authorization_code,
    fetch_oauth2_client_credentials_token,
    fetch_oauth2_refresh_access_token,
    post_oauth2_token_endpoint,
)
from astrocyte.recall.proxy import (
    PLACE_BANK,
    PLACE_QUERY,
    auth_with_oauth_cache_namespace,
    build_proxy_headers,
    fetch_proxy_recall_hits,
    gather_proxy_hits_for_bank,
    merge_manual_and_proxy_hits,
    validate_proxy_recall_dns,
    validate_proxy_recall_url,
)

__all__ = [
    "PLACE_BANK",
    "PLACE_QUERY",
    "auth_with_oauth_cache_namespace",
    "build_proxy_headers",
    "clear_oauth2_token_cache_for_tests",
    "exchange_oauth2_authorization_code",
    "fetch_oauth2_client_credentials_token",
    "fetch_oauth2_refresh_access_token",
    "fetch_proxy_recall_hits",
    "gather_proxy_hits_for_bank",
    "merge_manual_and_proxy_hits",
    "merge_external_into_recall_result",
    "post_oauth2_token_endpoint",
    "validate_proxy_recall_dns",
    "validate_proxy_recall_url",
]
