"""Webhook ingest: verify HMAC, parse JSON, call retain (M4).

HTTP adapters (FastAPI, Starlette, standalone gateway) should forward raw body bytes
and headers to :func:`handle_webhook_ingest`.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass

from astrocyte.config import SourceConfig
from astrocyte.errors import IngestError
from astrocyte.ingest.bank_resolve import resolve_ingest_bank_id
from astrocyte.ingest.hmac_auth import verify_hmac_sha256
from astrocyte.types import Metadata, RetainResult

RetainCallable = Callable[..., Awaitable[RetainResult]]


def _header_ci(headers: Mapping[str, str], name: str) -> str | None:
    want = name.lower()
    for k, v in headers.items():
        if k.lower() == want:
            return v
    return None


def _parse_json_body(raw: bytes) -> tuple[str, str | None, str, Metadata | None]:
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise IngestError(f"invalid JSON body: {e}") from e
    if not isinstance(data, dict):
        raise IngestError("JSON body must be an object")
    text = data.get("content") or data.get("text")
    if not text or not isinstance(text, str):
        raise IngestError("JSON must include string content or text")
    principal = data.get("principal")
    pr: str | None = str(principal).strip() if principal is not None else None
    ct = data.get("content_type") or "text"
    content_type = str(ct) if isinstance(ct, str) else "text"
    meta_raw = data.get("metadata")
    metadata = _coerce_metadata(meta_raw)
    return text, pr, content_type, metadata


def _coerce_metadata(meta_raw: object) -> Metadata | None:
    if not isinstance(meta_raw, dict):
        return None
    out: Metadata = {}
    for k, v in meta_raw.items():
        if isinstance(v, (str, int, float, bool)) or v is None:
            out[str(k)] = v  # type: ignore[assignment]
    return out if out else None


@dataclass(frozen=True)
class WebhookIngestResult:
    ok: bool
    http_status: int
    error: str | None = None
    retain_result: RetainResult | None = None


async def handle_webhook_ingest(
    *,
    source_id: str,
    source_config: SourceConfig,
    raw_body: bytes,
    headers: Mapping[str, str],
    retain: RetainCallable,
    principal: str | None = None,
) -> WebhookIngestResult:
    """Validate HMAC (when ``auth.type`` is ``hmac``), parse body, ``retain`` into target bank."""
    auth = source_config.auth or {}
    auth_type = (auth.get("type") or "").strip().lower()

    if auth_type == "hmac":
        secret = auth.get("secret")
        if not secret or not isinstance(secret, str):
            return WebhookIngestResult(ok=False, http_status=500, error="hmac secret not configured")
        header_name = str(auth.get("header") or "X-Astrocyte-Signature")
        sig = _header_ci(headers, header_name)
        if not sig:
            return WebhookIngestResult(ok=False, http_status=401, error="missing signature header")
        if not verify_hmac_sha256(secret, raw_body, sig):
            return WebhookIngestResult(ok=False, http_status=401, error="invalid signature")
    elif auth_type in ("", "none"):
        pass
    else:
        return WebhookIngestResult(ok=False, http_status=501, error=f"unsupported auth type: {auth_type!r}")

    try:
        text, json_principal, content_type, metadata = _parse_json_body(raw_body)
    except IngestError as e:
        return WebhookIngestResult(ok=False, http_status=400, error=str(e))

    eff_principal = principal or json_principal
    try:
        bank_id = resolve_ingest_bank_id(source_config, principal=eff_principal)
    except IngestError as e:
        return WebhookIngestResult(ok=False, http_status=400, error=str(e))

    profile = source_config.extraction_profile

    result = await retain(
        text,
        bank_id,
        metadata=metadata,
        content_type=content_type,
        extraction_profile=profile,
        source=source_id,
    )
    return WebhookIngestResult(ok=True, http_status=200, retain_result=result)
