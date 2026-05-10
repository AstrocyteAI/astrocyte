"""Shared R2 (Cloudflare S3-compatible) client setup for archive scripts.

All bench-archive tooling (`archive_bench_results.py`,
`fetch_bench_results.py`, `backfill_archive.py`) goes through this
module so credentials, endpoint construction, and bucket conventions
live in one place.

Reads six env vars (Doppler-injected; see
`docs/_plugins/benchmarks-doppler-setup.md`):

- ``R2_ACCOUNT_ID``
- ``R2_ACCESS_KEY_ID``
- ``R2_SECRET_ACCESS_KEY``
- ``R2_BUCKET``         — private bucket (raw runs)
- ``R2_BUCKET_PUBLIC``  — public bucket (trajectory artifact)
- ``R2_PUBLIC_URL``     — r2.dev URL the docs site reads from
"""
from __future__ import annotations

import os
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import AsyncIterator

try:
    from aiobotocore.session import get_session
except ImportError as exc:  # pragma: no cover
    raise ImportError(
        "aiobotocore is required for bench archive scripts. "
        "Install with: uv sync --extra dev"
    ) from exc


REQUIRED_VARS = (
    "R2_ACCOUNT_ID",
    "R2_ACCESS_KEY_ID",
    "R2_SECRET_ACCESS_KEY",
    "R2_BUCKET",
    "R2_BUCKET_PUBLIC",
    "R2_PUBLIC_URL",
)


@dataclass(frozen=True)
class R2Config:
    account_id: str
    access_key_id: str
    secret_access_key: str
    bucket_private: str
    bucket_public: str
    public_url: str

    @property
    def endpoint_url(self) -> str:
        return f"https://{self.account_id}.r2.cloudflarestorage.com"

    @classmethod
    def from_env(cls) -> "R2Config":
        missing = [v for v in REQUIRED_VARS if not os.environ.get(v)]
        if missing:
            raise RuntimeError(
                f"R2 archive disabled — missing env vars: {', '.join(missing)}. "
                "Run via `doppler run --config bench -- ...` or unset "
                "BENCH_ARCHIVE_DISABLE=1 to silence."
            )
        return cls(
            account_id=os.environ["R2_ACCOUNT_ID"],
            access_key_id=os.environ["R2_ACCESS_KEY_ID"],
            secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
            bucket_private=os.environ["R2_BUCKET"],
            bucket_public=os.environ["R2_BUCKET_PUBLIC"],
            public_url=os.environ["R2_PUBLIC_URL"].rstrip("/"),
        )


def env_is_configured() -> bool:
    """True iff all six R2_* env vars are present (post-run-hook gate)."""
    return all(os.environ.get(v) for v in REQUIRED_VARS)


@asynccontextmanager
async def r2_client(cfg: R2Config | None = None) -> AsyncIterator:
    """Yield an aiobotocore S3 client configured for R2.

    ``region_name="auto"`` is required for R2 — passing a real AWS region
    fails signature verification.
    """
    cfg = cfg or R2Config.from_env()
    session = get_session()
    async with session.create_client(
        "s3",
        endpoint_url=cfg.endpoint_url,
        aws_access_key_id=cfg.access_key_id,
        aws_secret_access_key=cfg.secret_access_key,
        region_name="auto",
    ) as client:
        yield client
