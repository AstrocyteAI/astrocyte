"""Parser ABC — converts raw file bytes to markdown.

Hindsight-style abstract base. Concrete parsers (Markdown, Markitdown,
LlamaParse) implement ``convert(bytes, filename) -> markdown text``.
Their output feeds the Document Engine's tree builders.

This is the layer that handles "user uploaded a file" → "we have text we
can parse into a tree." Markdown parsers pass through; PDF/DOCX/HTML
parsers extract text first.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


class UnsupportedFileTypeError(Exception):
    """Raised by a Parser when it can't handle a given file type."""


@dataclass
class ConvertResult:
    """Result of a successful file → markdown conversion."""

    content: str
    parser_name: str
    mime_type: str = "text/markdown"


class Parser(ABC):
    """Abstract base for file → markdown parsers.

    Subclasses MUST implement ``convert()`` and ``name()``. ``supports()``
    has a default of True; override for local extension-based filtering.
    Parsers that delegate to a remote service should leave ``supports()``
    True and raise ``UnsupportedFileTypeError`` from ``convert()``.
    """

    @abstractmethod
    async def convert(self, file_data: bytes, filename: str) -> str:
        """Convert file bytes to markdown text.

        Args:
            file_data: raw file bytes.
            filename: original filename (extension used for type detection).

        Returns:
            Markdown content (string).

        Raises:
            UnsupportedFileTypeError: this parser can't handle the file type.
            RuntimeError: parsing failed for some other reason.
        """

    @abstractmethod
    def name(self) -> str:
        """Short identifier for the parser, e.g. ``"markdown"``, ``"markitdown"``."""

    def supports(self, filename: str, content_type: str | None = None) -> bool:
        """Quick check: can this parser handle this file?

        Defaults to True (the parser handles everything until proven
        otherwise via UnsupportedFileTypeError). Override for static
        extension-based filtering.
        """
        return True
