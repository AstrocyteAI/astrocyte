"""HTTP proxy recall — fetch remote hits and merge with local RRF (M4.1)."""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import socket
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
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
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
                "proxy recall DNS resolved to a forbidden address "
                f"({addr_s!r} for host {host!r})",
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
