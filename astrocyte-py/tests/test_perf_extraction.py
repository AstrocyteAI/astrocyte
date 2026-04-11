"""Optional perf smoke for normalize + chunk (set ASTROCYTE_RUN_PERF=1)."""

from __future__ import annotations

import os
import time

import pytest

from astrocyte.pipeline.chunking import chunk_text
from astrocyte.pipeline.extraction import normalize_content

pytestmark = pytest.mark.skipif(
    os.environ.get("ASTROCYTE_RUN_PERF") != "1",
    reason="Set ASTROCYTE_RUN_PERF=1 to run perf smoke",
)


def test_normalize_and_chunk_large_text_finishes_quickly():
    body = ("Paragraph one. " * 80 + "\n\n") * 50
    assert len(body) > 100_000

    t0 = time.perf_counter()
    n = normalize_content(body, "document")
    chunks = chunk_text(n, strategy="paragraph", max_chunk_size=512)
    elapsed = time.perf_counter() - t0

    assert len(chunks) >= 1
    assert elapsed < 5.0, f"normalize+chunk took {elapsed:.2f}s (expected < 5s)"
