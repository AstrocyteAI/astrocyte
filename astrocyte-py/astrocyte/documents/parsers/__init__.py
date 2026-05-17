"""Parser registry + public re-exports.

Usage:
    from astrocyte.documents.parsers import ParserRegistry, MarkdownParser

    registry = ParserRegistry()
    registry.register(MarkdownParser())
    parser = registry.pick("notes.md")
    text = await parser.convert(file_bytes, "notes.md")
"""

from __future__ import annotations

import logging

from astrocyte.documents.parsers.base import (
    ConvertResult,
    Parser,
    UnsupportedFileTypeError,
)
from astrocyte.documents.parsers.markdown import MarkdownParser

__all__ = [
    "ConvertResult",
    "Parser",
    "UnsupportedFileTypeError",
    "MarkdownParser",
    "ParserRegistry",
]

logger = logging.getLogger(__name__)


class ParserRegistry:
    """Routes a file to the first registered parser that supports it.

    Registration order = preference order. Register more specific
    parsers first (e.g., a custom PDF parser) and the catch-all
    MarkdownParser last.
    """

    def __init__(self) -> None:
        self._parsers: list[Parser] = []

    def register(self, parser: Parser) -> None:
        self._parsers.append(parser)
        logger.debug("ParserRegistry: registered %s", parser.name())

    def pick(self, filename: str, content_type: str | None = None) -> Parser:
        """Return the first parser that supports the file.

        Raises ``UnsupportedFileTypeError`` if no registered parser
        supports the file. ``UnsupportedFileTypeError`` is also the
        right exception for ``convert()`` to raise downstream when a
        parser claimed support but then couldn't handle a specific file.
        """
        for p in self._parsers:
            if p.supports(filename, content_type):
                return p
        raise UnsupportedFileTypeError(
            f"No registered parser supports filename={filename!r} content_type={content_type!r}",
        )

    def names(self) -> list[str]:
        return [p.name() for p in self._parsers]

    def __len__(self) -> int:
        return len(self._parsers)


def default_registry() -> ParserRegistry:
    """A registry pre-populated with MarkdownParser only (Phase 1).

    Phase 6 will add MarkitdownParser + LlamaParseParser ahead of
    MarkdownParser so PDF/DOCX files route to richer parsers first
    and markdown falls through to the catch-all.
    """
    reg = ParserRegistry()
    reg.register(MarkdownParser())
    return reg
