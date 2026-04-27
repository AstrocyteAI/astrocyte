"""Text extraction utilities for S3 ingestion.

Supported formats:
  - Plain text  (.txt, .md, .rst, .csv, and any unrecognised text/*)
  - HTML        (.html, .htm)
  - PDF         (.pdf)    — requires pypdf
  - DOCX        (.docx)   — requires python-docx
"""

from __future__ import annotations

import io
import logging
from pathlib import PurePosixPath

logger = logging.getLogger("astrocyte_ingestion_s3")

# Maximum bytes we'll read into memory for a single object (50 MiB).
MAX_BYTES = 50 * 1024 * 1024


def _ext(key: str) -> str:
    return PurePosixPath(key).suffix.lower()


def extract_text(key: str, body: bytes, content_type: str | None = None) -> str | None:
    """Extract plain-text from *body* based on *key* extension and optional *content_type*.

    Returns ``None`` if the file is binary and has no supported extractor
    (so the caller can skip it cleanly).  Never raises — extraction errors
    are logged as warnings and return ``None``.
    """
    ext = _ext(key)
    ct = (content_type or "").lower().split(";")[0].strip()

    # ── PDF ────────────────────────────────────────────────────────
    if ext == ".pdf" or ct == "application/pdf":
        return _extract_pdf(key, body)

    # ── DOCX ───────────────────────────────────────────────────────
    if ext == ".docx" or ct == "application/vnd.openxmlformats-officedocument.wordprocessingml.document":
        return _extract_docx(key, body)

    # ── HTML ───────────────────────────────────────────────────────
    if ext in (".html", ".htm") or ct in ("text/html",):
        return _extract_html(key, body)

    # ── Plain text (and Markdown / RST / CSV) ──────────────────────
    if (
        ext in (".txt", ".md", ".markdown", ".rst", ".csv", ".log", ".yaml", ".yml", ".json", ".toml", ".xml")
        or ct.startswith("text/")
    ):
        return _extract_plain(key, body)

    # Unknown — skip silently
    logger.debug("s3 extract: skipping unsupported type key=%s ext=%s ct=%s", key, ext, ct)
    return None


# ---------------------------------------------------------------------------
# Format-specific helpers
# ---------------------------------------------------------------------------


def _extract_plain(key: str, body: bytes) -> str | None:
    for enc in ("utf-8", "latin-1"):
        try:
            return body.decode(enc)
        except UnicodeDecodeError:
            continue
    logger.warning("s3 extract: could not decode plain text key=%s", key)
    return None


def _extract_html(key: str, body: bytes) -> str | None:
    try:
        from bs4 import BeautifulSoup  # type: ignore[import-untyped]
    except ImportError:
        logger.warning("s3 extract: beautifulsoup4 not installed; falling back to raw text for %s", key)
        return _extract_plain(key, body)
    try:
        soup = BeautifulSoup(body, "html.parser")
        # Remove script / style
        for tag in soup(["script", "style", "noscript"]):
            tag.decompose()
        return soup.get_text(separator="\n", strip=True)
    except Exception as exc:
        logger.warning("s3 extract: html parse failed key=%s: %s", key, exc)
        return None


def _extract_pdf(key: str, body: bytes) -> str | None:
    try:
        from pypdf import PdfReader  # type: ignore[import-untyped]
    except ImportError:
        logger.warning("s3 extract: pypdf not installed; cannot extract PDF key=%s", key)
        return None
    try:
        reader = PdfReader(io.BytesIO(body))
        parts: list[str] = []
        for page in reader.pages:
            text = page.extract_text()
            if text:
                parts.append(text)
        return "\n".join(parts) or None
    except Exception as exc:
        logger.warning("s3 extract: pdf parse failed key=%s: %s", key, exc)
        return None


def _extract_docx(key: str, body: bytes) -> str | None:
    try:
        from docx import Document  # type: ignore[import-untyped]
    except ImportError:
        logger.warning("s3 extract: python-docx not installed; cannot extract DOCX key=%s", key)
        return None
    try:
        doc = Document(io.BytesIO(body))
        return "\n".join(p.text for p in doc.paragraphs if p.text.strip()) or None
    except Exception as exc:
        logger.warning("s3 extract: docx parse failed key=%s: %s", key, exc)
        return None
