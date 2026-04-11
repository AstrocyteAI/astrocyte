"""HTTP proxy recall — fetch remote hits and merge with local RRF (M4.1)."""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

import httpx

from astrocyte.config import SourceConfig
from astrocyte.policy.observability import MetricsCollector, span, timed
from astrocyte.types import MemoryHit, Metadata

if TYPE_CHECKING:
    from astrocyte.config import AstrocyteConfig

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT = 15.0

# JSON body placeholders (POST) — resolved to ``query`` / ``bank_id`` strings
PLACE_QUERY = "__astrocyte.query__"
PLACE_BANK = "__astrocyte.bank_id__"

_COUNTER = "astrocyte_proxy_recall_total"
_HIST = "astrocyte_proxy_recall_duration_seconds"


def _expand_proxy_url(template: str, query: str) -> str:
    if "{query}" in template:
        return template.replace("{query}", quote(query, safe=""))
    sep = "&" if "?" in template else "?"
    return f"{template}{sep}q={quote(query, safe='')}"


def auth_with_oauth_cache_namespace(
    auth: dict[str, str | int | float | bool | None] | None,
    source_id: str,
) -> dict[str, str | int | float | bool | None] | None:
    """Attach ``_oauth_cache_id`` so OAuth token caches do not collide across proxy sources."""
    if not auth:
        return None
    return {**auth, "_oauth_cache_id": source_id}


async def build_proxy_headers(
    auth: dict[str, str | int | float | bool | None] | None,
) -> dict[str, str]:
    """Build HTTP headers (Bearer, API key, OAuth2 client_credentials / refresh, optional static ``headers``)."""
    out: dict[str, str] = {}
    if not auth:
        return out
    extra = auth.get("headers")
    if isinstance(extra, dict):
        for k, v in extra.items():
            if isinstance(v, (str, int, float, bool)):
                out[str(k)] = str(v)
    t = (str(auth.get("type") or "")).strip().lower()
    grant = (str(auth.get("grant_type") or "")).strip().lower()
    if t in ("oauth2", "oauth2_client_credentials") and grant in ("", "client_credentials"):
        from astrocyte.recall.oauth import fetch_oauth2_client_credentials_token

        token = await fetch_oauth2_client_credentials_token(auth)
        out["Authorization"] = f"Bearer {token}"
    elif t == "oauth2_refresh" or (t in ("oauth2",) and grant == "refresh_token"):
        from astrocyte.recall.oauth import fetch_oauth2_refresh_access_token

        token = await fetch_oauth2_refresh_access_token(auth)
        out["Authorization"] = f"Bearer {token}"
    elif t == "bearer":
        token = auth.get("token")
        if token is not None and str(token).strip():
            out["Authorization"] = f"Bearer {token}"
    elif t == "api_key":
        header_name = str(auth.get("header") or "X-API-Key")
        val = auth.get("value") if auth.get("value") is not None else auth.get("token")
        if val is not None and str(val).strip():
            out[header_name] = str(val)
    return out


def _deep_replace_placeholders(obj: Any, query: str, bank_id: str) -> Any:
    if obj == PLACE_QUERY:
        return query
    if obj == PLACE_BANK:
        return bank_id
    if isinstance(obj, dict):
        return {k: _deep_replace_placeholders(v, query, bank_id) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_deep_replace_placeholders(x, query, bank_id) for x in obj]
    return obj


def _resolve_post_json(source: SourceConfig, query: str, bank_id: str) -> dict[str, Any]:
    rb = source.recall_body
    if rb is None:
        return {"query": query, "bank_id": bank_id}
    if isinstance(rb, dict):
        resolved = _deep_replace_placeholders(rb, query, bank_id)
        if isinstance(resolved, dict):
            return resolved
        return {"query": query, "bank_id": bank_id}
    if isinstance(rb, str):
        try:
            data = json.loads(rb)
        except json.JSONDecodeError:
            return {"query": query, "bank_id": bank_id}
        resolved = _deep_replace_placeholders(data, query, bank_id)
        if isinstance(resolved, dict):
            return resolved
        return {"query": query, "bank_id": bank_id}
    return {"query": query, "bank_id": bank_id}


def _row_to_hit(source_id: str, row: dict[str, Any]) -> MemoryHit | None:
    text = row.get("text")
    if not isinstance(text, str) or not text.strip():
        return None
    score = row.get("score")
    if isinstance(score, (int, float)):
        s = float(score)
    else:
        s = 0.5
    mid = row.get("memory_id")
    meta_raw = row.get("metadata")
    meta: Metadata | None = None
    if isinstance(meta_raw, dict):
        meta = {}
        for k, v in meta_raw.items():
            if isinstance(v, (str, int, float, bool)) or v is None:
                meta[str(k)] = v
    tags = row.get("tags")
    tag_list: list[str] | None = None
    if isinstance(tags, list):
        tag_list = [str(x) for x in tags]
    return MemoryHit(
        text=text,
        score=min(1.0, max(0.0, s)),
        fact_type=str(row["fact_type"]) if row.get("fact_type") is not None else None,
        metadata=meta,
        tags=tag_list,
        memory_id=str(mid) if mid is not None else None,
        source=f"proxy:{source_id}",
    )


def _parse_hits_payload(data: Any) -> list[Any]:
    raw_hits = data.get("hits") if isinstance(data, dict) else None
    if raw_hits is None and isinstance(data, dict):
        raw_hits = data.get("results")
    if not isinstance(raw_hits, list):
        return []
    return raw_hits


def _record_proxy_metrics(
    metrics: MetricsCollector | None,
    *,
    source_id: str,
    status: str,
    duration_s: float | None,
) -> None:
    if not metrics:
        return
    metrics.inc_counter(
        _COUNTER,
        {"source_id": source_id, "status": status},
        "Proxy recall attempts by source and status",
    )
    if duration_s is not None and status == "ok":
        metrics.observe_histogram(
            _HIST,
            duration_s,
            {"source_id": source_id},
            "Proxy recall HTTP duration in seconds",
        )


async def fetch_proxy_recall_hits(
    source_id: str,
    source: SourceConfig,
    *,
    query: str,
    bank_id: str,
    timeout: float = _DEFAULT_TIMEOUT,
    metrics: MetricsCollector | None = None,
) -> list[MemoryHit]:
    """Call ``source.url`` (GET or POST) and parse JSON ``hits`` / ``results`` arrays."""
    url_t = source.url or ""
    if not url_t.strip():
        return []

    method = (source.recall_method or "GET").strip().upper()
    if method not in ("GET", "POST"):
        logger.warning("proxy source %s: unknown recall_method %r, using GET", source_id, method)
        method = "GET"

    base_url = url_t.strip()

    with span(
        "astrocyte.proxy_recall",
        {"source_id": source_id, "method": method, "bank_id": bank_id},
    ):
        with timed() as t:
            try:
                headers = await build_proxy_headers(
                    auth_with_oauth_cache_namespace(source.auth, source_id),
                )
                async with httpx.AsyncClient(timeout=timeout) as client:
                    if method == "POST":
                        url = _expand_proxy_url(base_url, query) if "{query}" in base_url else base_url
                        body = _resolve_post_json(source, query, bank_id)
                        r = await client.post(url, headers=headers, json=body)
                    else:
                        url = _expand_proxy_url(base_url, query)
                        r = await client.get(url, headers=headers)
                    r.raise_for_status()
                    data = r.json()
            except Exception:
                _record_proxy_metrics(metrics, source_id=source_id, status="error", duration_s=None)
                raise
        duration_s = t["elapsed_ms"] / 1000.0

    _record_proxy_metrics(metrics, source_id=source_id, status="ok", duration_s=duration_s)

    raw_hits = _parse_hits_payload(data)
    out: list[MemoryHit] = []
    for row in raw_hits:
        if not isinstance(row, dict):
            continue
        hit = _row_to_hit(source_id, row)
        if hit:
            out.append(hit)
    return out


async def gather_proxy_hits_for_bank(
    config: AstrocyteConfig | Any,
    *,
    query: str,
    bank_id: str,
    metrics: MetricsCollector | None = None,
) -> list[MemoryHit]:
    """Fetch hits from all ``type: proxy`` sources whose ``target_bank`` matches ``bank_id``."""
    sources = getattr(config, "sources", None) or {}
    out: list[MemoryHit] = []
    for sid, src in sources.items():
        if not isinstance(src, SourceConfig):
            continue
        if (src.type or "").strip().lower() != "proxy":
            continue
        if (src.target_bank or "").strip() != bank_id:
            continue
        try:
            batch = await fetch_proxy_recall_hits(sid, src, query=query, bank_id=bank_id, metrics=metrics)
            out.extend(batch)
        except Exception as e:
            logger.warning("proxy recall failed for source %s: %s", sid, e)
    return out


async def merge_manual_and_proxy_hits(
    config: AstrocyteConfig | Any,
    *,
    query: str,
    bank_id: str,
    manual: list[MemoryHit] | None,
    metrics: MetricsCollector | None = None,
) -> list[MemoryHit] | None:
    """Combine caller ``external_context`` with configured proxy sources for this bank."""
    proxy = await gather_proxy_hits_for_bank(config, query=query, bank_id=bank_id, metrics=metrics)
    if not proxy and not manual:
        return None
    return list(manual or []) + proxy
