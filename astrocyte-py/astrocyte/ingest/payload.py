"""Shared JSON / field parsing for webhook and stream ingest (M4+)."""

from __future__ import annotations

import json
from collections.abc import Mapping

from astrocyte.errors import IngestError
from astrocyte.types import Metadata


def _coerce_metadata(meta_raw: object) -> Metadata | None:
    if not isinstance(meta_raw, dict):
        return None
    out: Metadata = {}
    for k, v in meta_raw.items():
        if isinstance(v, (str, int, float, bool)) or v is None:
            out[str(k)] = v  # type: ignore[assignment]
    return out if out else None


def parse_ingest_json_object(data: dict) -> tuple[str, str | None, str, Metadata | None]:
    """Parse the webhook-style JSON object (``content``/``text``, optional ``principal``, …)."""
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


def parse_ingest_stream_fields(fields: Mapping[str, str]) -> tuple[str, str | None, str, Metadata | None]:
    """Parse Redis Stream message fields into retain inputs.

    * If field ``payload`` is present, it must be a JSON string of an object — same schema as
      :func:`parse_ingest_json_object`.
    * Otherwise ``content`` or ``text`` is required; optional ``principal``, ``content_type``,
      ``metadata`` (JSON string).
    """
    if "payload" in fields:
        raw = fields["payload"]
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            raise IngestError(f"invalid payload JSON: {e}") from e
        if not isinstance(data, dict):
            raise IngestError("payload must be a JSON object")
        return parse_ingest_json_object(data)

    text = fields.get("content") or fields.get("text")
    if not text:
        raise IngestError("stream fields must include content or text, or a payload field")
    pr_raw = fields.get("principal")
    pr: str | None = str(pr_raw).strip() if pr_raw else None
    ct = fields.get("content_type") or "text"
    content_type = str(ct) if ct else "text"
    meta_s = fields.get("metadata")
    metadata: Metadata | None = None
    if meta_s:
        try:
            parsed = json.loads(meta_s)
        except json.JSONDecodeError as e:
            raise IngestError(f"invalid metadata JSON: {e}") from e
        metadata = _coerce_metadata(parsed)
    return text, pr, content_type, metadata


def parse_ingest_kafka_value(value: bytes | None) -> tuple[str, str | None, str, Metadata | None]:
    """Decode a Kafka record value: JSON object (webhook-shaped) or raw UTF-8 text."""
    if not value:
        raise IngestError("kafka message value is empty")
    s = value.decode("utf-8")
    try:
        data = json.loads(s)
        if isinstance(data, dict):
            return parse_ingest_json_object(data)
    except json.JSONDecodeError:
        # Value is not JSON; treat the decoded string as plain text (see return below).
        pass
    return s, None, "text", None
