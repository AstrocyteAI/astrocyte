"""MarkitdownParser — PDF/DOCX/HTML/PPTX → Markdown via markitdown.

Microsoft's markitdown library (Apache 2.0, local, no API key required).
Produces richer section structure than raw text extraction for text-heavy
PDFs: headings are preserved from document structure (PDF bookmarks,
DOCX heading styles) rather than inferred from page boundaries.

Install: pip install markitdown
     or: pip install 'astrocyte[markitdown]'
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from astrocyte.documents.parsers.base import Parser, UnsupportedFileTypeError

_SUPPORTED_EXTENSIONS = frozenset({".pdf", ".docx", ".pptx", ".html", ".htm", ".xlsx"})
_SUPPORTED_MIME_PREFIXES = (
    "application/pdf",
    "application/vnd.openxmlformats",
    "application/vnd.ms-",
    "text/html",
    "application/msword",
)


class MarkitdownParser(Parser):
    """PDF/DOCX/HTML/PPTX → Markdown via markitdown (Microsoft, Apache 2.0).

    Local — no network calls, no API key.

    Compared to the pymupdf fallback in the bench harness, markitdown
    preserves document heading structure (PDF bookmarks → markdown headers)
    so build_markdown_tree produces semantically meaningful section
    boundaries rather than one leaf per page.
    """

    def name(self) -> str:
        return "markitdown"

    def supports(self, filename: str, content_type: str | None = None) -> bool:
        if content_type:
            for prefix in _SUPPORTED_MIME_PREFIXES:
                if content_type.lower().startswith(prefix):
                    return True
        if filename:
            return Path(filename).suffix.lower() in _SUPPORTED_EXTENSIONS
        return False

    async def convert(self, file_data: bytes, filename: str) -> str:
        """Convert file bytes to markdown via markitdown.

        Writes to a temp file (markitdown detects format by extension),
        converts, then cleans up. The temp file is always removed even
        on error.

        Raises:
            UnsupportedFileTypeError: markitdown is not installed.
            RuntimeError: markitdown could not parse this file.
        """
        try:
            from markitdown import MarkItDown  # type: ignore[import-not-found]
        except ImportError as exc:
            raise UnsupportedFileTypeError(
                "markitdown is not installed. "
                "Install: pip install markitdown  "
                "or: pip install 'astrocyte[markitdown]'"
            ) from exc

        ext = Path(filename).suffix or ".pdf"
        tmp_path: str | None = None
        try:
            with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp:
                tmp.write(file_data)
                tmp_path = tmp.name

            md = MarkItDown()
            result = md.convert(tmp_path)
            return result.text_content or ""
        except UnsupportedFileTypeError:
            raise
        except Exception as exc:
            raise RuntimeError(
                f"markitdown failed to convert {filename!r}: {exc}"
            ) from exc
        finally:
            if tmp_path is not None:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
