"""Token-bucket rate limiter for concurrent eval question batches.

Combines asyncio.Semaphore (cap on concurrent in-flight questions) with
dual smooth token buckets (RPM + TPM) to stay within OpenAI API quota
without serialising evaluation.

Usage:
    limiter = EvalRateLimiter(max_concurrency=5, rpm=500, tpm=200_000)
    async with limiter:
        result = await brain.recall(...)
"""

from __future__ import annotations

import asyncio
import time


class EvalRateLimiter:
    """Semaphore + dual token-bucket (RPM + TPM) rate limiter.

    Token buckets refill continuously (leaky-bucket style) to prevent the
    burst-at-top-of-minute problem that discrete-window limiters cause.
    Each acquire() charges `tokens_per_question` against the TPM bucket
    and 1 against the RPM bucket. Blocks until both have headroom.

    Args:
        max_concurrency: Maximum number of questions evaluated in parallel.
        rpm: Requests per minute budget shared across all workers.
        tpm: Tokens per minute budget shared across all workers.
        tokens_per_question: Estimated token cost per question (input+output).
            Tune lower to be more conservative, higher to be more aggressive.
    """

    def __init__(
        self,
        max_concurrency: int = 5,
        rpm: int = 500,
        tpm: int = 200_000,
        tokens_per_question: int = 3_000,
    ) -> None:
        self._sem = asyncio.Semaphore(max_concurrency)
        self._rpm = rpm
        self._tpm = tpm
        self._tokens_per_question = tokens_per_question
        # Start buckets full
        self._req_bucket: float = float(rpm)
        self._tok_bucket: float = float(tpm)
        self._last_refill: float = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        """Block until a concurrency slot and rate-limit tokens are available."""
        await self._sem.acquire()
        await self._wait_for_tokens()

    def release(self) -> None:
        self._sem.release()

    async def __aenter__(self) -> "EvalRateLimiter":
        await self.acquire()
        return self

    async def __aexit__(self, *_: object) -> None:
        self.release()

    async def _wait_for_tokens(self) -> None:
        while True:
            async with self._lock:
                now = time.monotonic()
                elapsed = now - self._last_refill
                # Continuous per-second refill (not per-minute window)
                self._req_bucket = min(
                    float(self._rpm),
                    self._req_bucket + elapsed * self._rpm / 60.0,
                )
                self._tok_bucket = min(
                    float(self._tpm),
                    self._tok_bucket + elapsed * self._tpm / 60.0,
                )
                self._last_refill = now
                if self._req_bucket >= 1.0 and self._tok_bucket >= self._tokens_per_question:
                    self._req_bucket -= 1.0
                    self._tok_bucket -= float(self._tokens_per_question)
                    return
            await asyncio.sleep(0.05)
