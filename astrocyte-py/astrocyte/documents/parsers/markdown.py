"""MarkdownParser — pass-through for markdown / text input.

Decodes UTF-8 bytes to a string and returns the markdown unchanged.
The cheapest parser; useful for tests, for inline content, and as the
zero-configuration default when ingesting text the caller already has.
"""

from __future__ import annotations

from astrocyte.documents.parsers.base import Parser

_MARKDOWN_EXTENSIONS = {".md", ".markdown", ".txt", ".rst"}


class MarkdownParser(Parser):
    """Treat input as raw markdown / plain text. UTF-8 decode + return."""

    def name(self) -> str:
        return "markdown"

    def supports(self, filename: str, content_type: str | None = None) -> bool:
        if content_type and content_type.lower().startswith(("text/markdown", "text/plain")):
            return True
        if not filename:
            return True  # if we don't know, claim it
        lower = filename.lower()
        return any(lower.endswith(ext) for ext in _MARKDOWN_EXTENSIONS)

    async def convert(self, file_data: bytes, filename: str) -> str:
        """Decode bytes as UTF-8 markdown; pass through.

        Surrogate-escape fallback for malformed encodings so we never
        raise on a slightly-broken file — the tree builder will handle
        whatever comes out.
        """
        try:
            return file_data.decode("utf-8")
        except UnicodeDecodeError:
            return file_data.decode("utf-8", errors="surrogateescape")
