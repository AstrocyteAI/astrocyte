"""Text extraction for the document folder adapter.

Supported formats:
  - Plain text  (.txt, .md, .rst, .csv, …)
  - HTML        (.html, .htm)
  - PDF         (.pdf)    — requires pypdf
  - DOCX        (.docx)   — requires python-docx
"""

from __future__ import annotations

import io
import logging
from pathlib import Path

logger = logging.getLogger("astrocyte_ingestion_document")

# Maximum file size we'll read (50 MiB).
MAX_BYTES = 50 * 1024 * 1024

_TEXT_EXTENSIONS = {
    ".txt", ".md", ".markdown", ".rst", ".csv", ".log",
    ".yaml", ".yml", ".json", ".toml", ".xml",
}


def extract_text(path: Path) -> str | None:
    """Extract plain text from *path*.

    Returns ``None`` if the file type is unsupported or extraction fails.
    """
    ext = path.suffix.lower()

    if path.stat().st_size > MAX_BYTES:
        logger.warning("document extract: file too large (>%d bytes), skipping %s", MAX_BYTES, path)
        return None

    body = path.read_bytes()

    if ext == ".pdf":
        return _extract_pdf(path, body)
    if ext == ".docx":
        return _extract_docx(path, body)
    if ext in (".html", ".htm"):
        return _extract_html(path, body)
    if ext in _TEXT_EXTENSIONS:
        return _extract_plain(path, body)

    # Try plain-text decode for unknown extensions as a best-effort
    logger.debug("document extract: unknown extension %s, attempting plain-text decode", ext)
    return _extract_plain(path, body)


def _extract_plain(path: Path, body: bytes) -> str | None:
    for enc in ("utf-8", "latin-1"):
        try:
            return body.decode(enc)
        except UnicodeDecodeError:
            continue
    logger.warning("document extract: could not decode %s as text", path)
    return None


def _extract_html(path: Path, body: bytes) -> str | None:
    try:
        from bs4 import BeautifulSoup  # type: ignore[import-untyped]
    except ImportError:
        logger.warning("document extract: beautifulsoup4 not installed, falling back to raw text for %s", path)
        return _extract_plain(path, body)
    try:
        soup = BeautifulSoup(body, "html.parser")
        for tag in soup(["script", "style", "noscript"]):
            tag.decompose()
        return soup.get_text(separator="\n", strip=True)
    except Exception as exc:
        logger.warning("document extract: html parse failed for %s: %s", path, exc)
        return None


def _extract_pdf(path: Path, body: bytes) -> str | None:
    try:
        from pypdf import PdfReader  # type: ignore[import-untyped]
    except ImportError:
        logger.warning("document extract: pypdf not installed; cannot read PDF %s", path)
        return None
    try:
        reader = PdfReader(io.BytesIO(body))
        parts = [page.extract_text() for page in reader.pages if page.extract_text()]
        return "\n".join(parts) or None
    except Exception as exc:
        logger.warning("document extract: pdf parse failed for %s: %s", path, exc)
        return None


def _extract_docx(path: Path, body: bytes) -> str | None:
    try:
        from docx import Document  # type: ignore[import-untyped]
    except ImportError:
        logger.warning("document extract: python-docx not installed; cannot read DOCX %s", path)
        return None
    try:
        doc = Document(io.BytesIO(body))
        return "\n".join(p.text for p in doc.paragraphs if p.text.strip()) or None
    except Exception as exc:
        logger.warning("document extract: docx parse failed for %s: %s", path, exc)
        return None
