"""S3/Garage :class:`~astrocyte.ingest.source.IngestSource` adapters for Astrocyte."""

from astrocyte_ingestion_s3.source import S3PollIngestSource, S3WebhookIngestSource

__all__ = ["S3PollIngestSource", "S3WebhookIngestSource"]
