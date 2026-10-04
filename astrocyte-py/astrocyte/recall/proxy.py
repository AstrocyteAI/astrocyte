"""HTTP proxy recall — fetch remote hits and merge with local RRF (M4.1)."""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import os
import re
import socket
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any
from urllib.parse import ParseResult, parse_qsl, quote, urlparse, urlunparse

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


def _forbidden_proxy_target_ip_obj(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """True if this address must not be used for outbound proxy recall (SSRF mitigation)."""
    return bool(
        ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_unspecified
    )


def _unsafe_literal_ip(host: str) -> bool:
    """True if *host* parses as an IPv4/IPv6 address that must not be used for proxy recall."""
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return _forbidden_proxy_target_ip_obj(ip)


def validate_proxy_recall_url(url: str) -> None:
    """Reject URLs that enable SSRF (private/loopback/metadata-ranged IPs, non-HTTP(S), no host).

    Call this on the **fully expanded** request URL (including encoded user ``query`` fragments).
    Hostnames still require :func:`validate_proxy_recall_dns` before HTTP (rebinding-safe check).
    """
    if not (url or "").strip():
        raise ValueError("proxy recall URL is empty")
    parsed = urlparse(url.strip())
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"proxy recall URL scheme not allowed: {parsed.scheme!r}")
    host = parsed.hostname
    if not host:
        raise ValueError("proxy recall URL has no host")
    h = host.lower().rstrip(".")
    if h == "localhost" or h.endswith(".localhost"):
        raise ValueError("proxy recall URL must not target localhost")
    if _unsafe_literal_ip(h):
        raise ValueError(
            "proxy recall URL must not target loopback, private, link-local, or reserved addresses",
        )


def _sync_dns_validate_and_first_public_ip(
    host: str,
    port: int,
) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    """Resolve *host*, reject if any address is forbidden, return the first (OS order) for pinning."""
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as e:
        raise ValueError(f"proxy recall DNS resolution failed for {host!r}: {e}") from e
    if not infos:
        raise ValueError(f"proxy recall DNS returned no addresses for {host!r}")
    picked: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
    for _fam, _socktype, _proto, _canon, sockaddr in infos:
        addr_s = sockaddr[0]
        try:
            ip = ipaddress.ip_address(addr_s)
        except ValueError:
            continue
        if _forbidden_proxy_target_ip_obj(ip):
            raise ValueError(
                f"proxy recall DNS resolved to a forbidden address ({addr_s!r} for host {host!r})",
            )
        picked.append(ip)
    if not picked:
        raise ValueError(f"proxy recall DNS returned no usable addresses for {host!r}")
    return picked[0]


def _is_literal_ip_host(host: str) -> bool:
    """True if *host* is an IPv4/IPv6 literal (no DNS name)."""
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


def _proxy_recall_host_header_value(original_hostname: str, port: int, scheme: str) -> str:
    """``Host`` header for the original server name (include port when not default)."""
    sch = (scheme or "").lower()
    default = 443 if sch == "https" else 80
    h = original_hostname.lower().rstrip(".")
    if port != default:
        return f"{h}:{port}"
    return h


def _rebuild_request_url_pinned_to_ip(
    parsed: ParseResult,
    *,
    pinned: ipaddress.IPv4Address | ipaddress.IPv6Address,
    port: int,
) -> str:
    """Replace authority with pinned IP so the TCP/TLS layer cannot re-resolve differently."""
    if isinstance(pinned, ipaddress.IPv6Address):
        hostpart = f"[{pinned.compressed}]"
    else:
        hostpart = str(pinned)
    sch = (parsed.scheme or "").lower()
    default = 443 if sch == "https" else 80
    if port != default:
        netloc = f"{hostpart}:{port}"
    else:
        netloc = hostpart
    return urlunparse((parsed.scheme, netloc, parsed.path, parsed.params, parsed.query, parsed.fragment))


def _httpx_url_and_query_params(url: str) -> tuple[str, list[tuple[str, str]] | None]:
    """Split *url* into a path-only URL for the httpx *url* argument and query pairs for *params*.

    User-controlled search text must not be concatenated into the URL string passed to httpx
    (SSRF static analysis + clearer separation of authority vs query).
    """
    p = urlparse((url or "").strip())
    without_query = urlunparse((p.scheme, p.netloc, p.path, p.params, "", p.fragment))
    if not p.query:
        return without_query, None
    pairs = parse_qsl(p.query, keep_blank_values=True)
    if not pairs:
        return without_query, None
    return without_query, list(pairs)


async def validate_proxy_recall_dns(host: str, port: int) -> None:
    """Async-safe DNS check: resolve *host* in a worker thread, then validate all addresses."""
    await asyncio.to_thread(_sync_dns_validate_and_first_public_ip, host, port)


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
        if not re.match(r"^[A-Za-z0-9\-]+$", header_name):
            raise ValueError(f"Invalid header name in proxy auth config: {header_name!r}")
        val = auth.get("value") if auth.get("value") is not None else auth.get("token")
        if val is not None and str(val).strip():
            val_str = str(val)
            if "\r" in val_str or "\n" in val_str:
                raise ValueError("Header value contains CRLF characters (possible injection)")
            out[header_name] = val_str
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


#: Provenance fields a remote row may carry, mapped onto reserved metadata keys.
#: Reserved keys are written last, so a source cannot spoof them through its own
#: ``metadata`` object.
_PROVENANCE_FIELDS: dict[str, tuple[str, ...]] = {
    "_source_url": ("url", "source_url"),
    "_source_version": ("version", "etag", "revision"),
    "_source_author": ("author",),
    "_source_anchor": ("anchor",),
}


def _parse_when(value: Any) -> datetime | None:
    """ISO-8601 string or Unix seconds -> aware UTC datetime; anything else -> None."""
    if isinstance(value, bool):
        return None
    try:
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(float(value), tz=timezone.utc)
        if isinstance(value, str) and value.strip():
            dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (ValueError, OverflowError, OSError):
        return None
    return None


def _row_to_hit(source_id: str, row: dict[str, Any]) -> MemoryHit | None:
    """Map one remote row to a ``MemoryHit`` with the dates and provenance a
    local hit carries (federated-sources F0b).

    Before 2026-10-04 only text, score, flat metadata, tags, id, and fact type
    survived, so every federated hit arrived undated and unanchored: the same
    class of defect as a rerank that dropped ``occurred_at`` (roadmap §4e).
    Fusion ranks by list position, so a missing score never ranked anything;
    it gets a neutral placeholder flagged ``_score_missing`` rather than
    passing as a measurement.
    """
    text = row.get("text")
    if not isinstance(text, str) or not text.strip():
        return None
    score = row.get("score")
    score_missing = isinstance(score, bool) or not isinstance(score, (int, float))
    s = 0.5 if score_missing else float(score)
    mid = row.get("memory_id")
    meta: Metadata = {}
    meta_raw = row.get("metadata")
    if isinstance(meta_raw, dict):
        for k, v in meta_raw.items():
            if isinstance(v, (str, int, float, bool)) or v is None:
                meta[str(k)] = v
    for key, names in _PROVENANCE_FIELDS.items():
        meta.pop(key, None)
        for name in names:
            v = row.get(name)
            if isinstance(v, (str, int, float)) and not isinstance(v, bool) and str(v).strip():
                meta[key] = str(v)
                break
    if score_missing:
        meta["_score_missing"] = True
    else:
        meta.pop("_score_missing", None)
    tags = row.get("tags")
    tag_list: list[str] | None = None
    if isinstance(tags, list):
        tag_list = [str(x) for x in tags]
    return MemoryHit(
        text=text,
        score=min(1.0, max(0.0, s)),
        fact_type=str(row["fact_type"]) if row.get("fact_type") is not None else None,
        metadata=meta or None,
        tags=tag_list,
        memory_id=str(mid) if mid is not None else None,
        source=f"proxy:{source_id}",
        occurred_at=_parse_when(row.get("occurred_at")),
        # When the source last recorded it: its own retained_at, else the
        # document's last modification.
        retained_at=_parse_when(row.get("retained_at")) or _parse_when(row.get("updated_at")),
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
    # Every timed outcome, not only successes: a histogram of successful calls
    # alone hides exactly the slow tail (errors after a long wait, deadline
    # misses) that p95 is meant to expose. ``status`` stays on the counter, so
    # the documented label set (ADR-003) is unchanged.
    if duration_s is not None:
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
    started = time.monotonic()

    with span(
        "astrocyte.proxy_recall",
        {"source_id": source_id, "method": method, "bank_id": bank_id},
    ):
        with timed() as t:
            try:
                headers = await build_proxy_headers(
                    auth_with_oauth_cache_namespace(source.auth, source_id),
                )
                if method == "POST":
                    request_url = _expand_proxy_url(base_url, query) if "{query}" in base_url else base_url
                    body = _resolve_post_json(source, query, bank_id)
                else:
                    request_url = _expand_proxy_url(base_url, query)
                    body = None
                validate_proxy_recall_url(request_url)
                parsed_req = urlparse(request_url)
                req_host = parsed_req.hostname
                if not req_host:
                    raise ValueError("proxy recall URL has no host")
                req_port = parsed_req.port or (443 if parsed_req.scheme == "https" else 80)
                sch = (parsed_req.scheme or "").lower()

                pinned = await asyncio.to_thread(
                    _sync_dns_validate_and_first_public_ip,
                    req_host,
                    req_port,
                )

                if _is_literal_ip_host(req_host):
                    effective_url = request_url
                    out_headers = headers
                    req_extensions: dict[str, Any] | None = None
                else:
                    effective_url = _rebuild_request_url_pinned_to_ip(
                        parsed_req,
                        pinned=pinned,
                        port=req_port,
                    )
                    out_headers = {
                        **headers,
                        "Host": _proxy_recall_host_header_value(req_host, req_port, sch),
                    }
                    req_extensions = {"sni_hostname": req_host} if sch == "https" else None

                httpx_url, httpx_params = _httpx_url_and_query_params(effective_url)

                async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
                    if method == "POST":
                        r = await client.request(
                            "POST",
                            httpx_url,
                            params=httpx_params,
                            headers=out_headers,
                            json=body,
                            extensions=req_extensions,
                        )
                    else:
                        r = await client.request(
                            "GET",
                            httpx_url,
                            params=httpx_params,
                            headers=out_headers,
                            extensions=req_extensions,
                        )
                    r.raise_for_status()
                    data = r.json()
            except Exception:
                _record_proxy_metrics(
                    metrics, source_id=source_id, status="error", duration_s=time.monotonic() - started
                )
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


#: One deadline for all proxy sources of a recall, in seconds. Sources run
#: concurrently; whatever has answered by the deadline is fused and the rest
#: are cancelled. Before 2026-10-04 sources ran one after another with 15 s
#: each, so recall paid the SUM of every remote call (federated-sources §1.1).
_DEFAULT_DEADLINE = 0.8
#: Consecutive failures (errors or timeouts) before a source is skipped.
_BREAKER_THRESHOLD = 3
#: How long a tripped source is skipped before it is tried again.
_BREAKER_COOLDOWN_S = 60.0

# source_id -> (consecutive failures, monotonic time until which it is skipped)
_breakers: dict[str, tuple[int, float]] = {}

#: A source that misses the deadline keeps running in the background; when it
#: answers, its hits are kept here for the next recall of the same query, so
#: the work is not thrown away. One-shot: a cached answer is used once.
#: Keyed by (source, bank, query): sources authenticate with their own config
#: today, not per caller. Per-caller auth (federated-sources F4) must add the
#: principal to the key, or one caller would be served another's results.
_LATE_TTL_S = 60.0
_LATE_MAX_ENTRIES = 256
#: A late fetch is cancelled outright after this many deadlines.
_LATE_HARD_CAP_FACTOR = 10.0
_late_hits: dict[tuple[str, str, str], tuple[float, list[MemoryHit]]] = {}
_late_tasks: set[asyncio.Task] = set()


def _deadline_seconds() -> float:
    raw = os.environ.get("ASTROCYTE_PROXY_RECALL_DEADLINE_SECONDS")
    try:
        value = float(raw) if raw else _DEFAULT_DEADLINE
    except ValueError:
        logger.warning("ignoring invalid ASTROCYTE_PROXY_RECALL_DEADLINE_SECONDS=%r", raw)
        value = _DEFAULT_DEADLINE
    return value if value > 0 else _DEFAULT_DEADLINE


def _breaker_open(source_id: str) -> bool:
    _failures, until = _breakers.get(source_id, (0, 0.0))
    return time.monotonic() < until


def _record_outcome(source_id: str, ok: bool) -> None:
    if ok:
        _breakers.pop(source_id, None)
        return
    failures = _breakers.get(source_id, (0, 0.0))[0] + 1
    until = time.monotonic() + _BREAKER_COOLDOWN_S if failures >= _BREAKER_THRESHOLD else 0.0
    if until:
        logger.warning(
            "proxy source %s failed %d times in a row; skipping it for %.0f s",
            source_id, failures, _BREAKER_COOLDOWN_S,
        )
    _breakers[source_id] = (failures, until)


def reset_proxy_breakers() -> None:
    """Forget every source's failure history and late answers (tests, config reloads)."""
    _breakers.clear()
    _late_hits.clear()
    for task in list(_late_tasks):
        task.cancel()
    _late_tasks.clear()


def _take_late(key: tuple[str, str, str]) -> list[MemoryHit] | None:
    entry = _late_hits.pop(key, None)
    if entry is None:
        return None
    expires, hits = entry
    return hits if time.monotonic() < expires else None


def _keep_late(key: tuple[str, str, str], task: asyncio.Task, hard_cap: asyncio.TimerHandle) -> None:
    hard_cap.cancel()
    _late_tasks.discard(task)
    if task.cancelled() or task.exception() is not None:
        return
    hits = task.result()
    if not hits:
        return
    _late_hits[key] = (time.monotonic() + _LATE_TTL_S, hits)
    while len(_late_hits) > _LATE_MAX_ENTRIES:
        _late_hits.pop(next(iter(_late_hits)))


async def gather_proxy_hits_for_bank(
    config: AstrocyteConfig | Any,
    *,
    query: str,
    bank_id: str,
    metrics: MetricsCollector | None = None,
) -> list[MemoryHit]:
    """Fetch hits from all ``type: proxy`` sources whose ``target_bank`` matches ``bank_id``.

    Sources are queried concurrently under one deadline; a source that has not
    answered by then contributes nothing to this recall, so one slow or dead
    source costs at most the deadline. It keeps running in the background (up
    to ten deadlines), and a late answer serves the next recall of the same
    query. A source that keeps failing or missing the deadline is skipped for a
    cool-down. Hits come back in config order, not completion order, so
    fusion ranks do not depend on network timing.
    """
    sources = getattr(config, "sources", None) or {}
    eligible: list[tuple[str, SourceConfig]] = []
    for sid, src in sources.items():
        if not isinstance(src, SourceConfig):
            continue
        if (src.type or "").strip().lower() != "proxy":
            continue
        if (src.target_bank or "").strip() != bank_id:
            continue
        if _breaker_open(sid):
            _record_proxy_metrics(metrics, source_id=sid, status="skipped", duration_s=None)
            continue
        eligible.append((sid, src))
    if not eligible:
        return []

    deadline = _deadline_seconds()

    async def one(sid: str, src: SourceConfig) -> list[MemoryHit] | None:
        """Hits, or None when the source failed."""
        cap = src.recall_timeout_seconds
        timeout = min(cap, deadline) if cap and cap > 0 else deadline
        try:
            return await fetch_proxy_recall_hits(
                sid, src, query=query, bank_id=bank_id, timeout=timeout, metrics=metrics
            )
        except Exception as e:
            logger.warning("proxy recall failed for source %s: %s", sid, e)
            return None

    results: dict[str, list[MemoryHit]] = {}
    tasks: dict[str, asyncio.Task] = {}
    for sid, src in eligible:
        late = _take_late((sid, bank_id, query))
        if late is not None:
            results[sid] = late
            _record_proxy_metrics(metrics, source_id=sid, status="late_cache", duration_s=None)
        else:
            tasks[sid] = asyncio.create_task(one(sid, src))

    if tasks:
        _done, pending = await asyncio.wait(tasks.values(), timeout=deadline)
        loop = asyncio.get_running_loop()
        for sid, task in tasks.items():
            if task in pending:
                # Missing the deadline counts against the source even if it
                # answers later: a source that is always late must still trip
                # the breaker. Its late answer only fills the cache.
                _record_outcome(sid, ok=False)
                _record_proxy_metrics(metrics, source_id=sid, status="timeout", duration_s=deadline)
                logger.warning("proxy source %s missed the %.2f s recall deadline", sid, deadline)
                hard_cap = loop.call_later(deadline * _LATE_HARD_CAP_FACTOR, task.cancel)
                _late_tasks.add(task)
                task.add_done_callback(
                    lambda t, k=(sid, bank_id, query), h=hard_cap: _keep_late(k, t, h)
                )
                continue
            hits = task.result()
            _record_outcome(sid, ok=hits is not None)
            if hits:
                results[sid] = hits

    out: list[MemoryHit] = []
    for sid, _src in eligible:
        out.extend(results.get(sid, []))
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
