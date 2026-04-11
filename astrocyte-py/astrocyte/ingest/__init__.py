"""M4 — external data ingest (webhooks first; streams/polls later).

Use :func:`astrocyte.ingest.webhook.handle_webhook_ingest` from an HTTP layer with the raw
request body (needed for HMAC). See ``docs/_design/product-roadmap-v1.md`` (M4).
"""

from astrocyte.ingest.bank_resolve import resolve_ingest_bank_id
from astrocyte.ingest.hmac_auth import compute_hmac_sha256_hex, verify_hmac_sha256
from astrocyte.ingest.registry import SourceRegistry
from astrocyte.ingest.source import IngestSource, WebhookIngestSource
from astrocyte.ingest.webhook import WebhookIngestResult, handle_webhook_ingest

__all__ = [
    "IngestSource",
    "WebhookIngestSource",
    "SourceRegistry",
    "handle_webhook_ingest",
    "WebhookIngestResult",
    "resolve_ingest_bank_id",
    "compute_hmac_sha256_hex",
    "verify_hmac_sha256",
]
