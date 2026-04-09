"""Text chunking — split content into memory-sized pieces.

Sync, pure computation — Rust migration candidate.
See docs/_design/built-in-pipeline.md section 2.
"""

from __future__ import annotations

import re


def chunk_text(
    text: str,
    strategy: str = "sentence",
    max_chunk_size: int = 512,
    overlap: int = 50,
) -> list[str]:
    """Split text into chunks using the specified strategy.

    Strategies:
        - "sentence": split on sentence boundaries (.!?)
        - "paragraph": split on double newlines
        - "fixed": fixed character count with overlap

    Returns list of non-empty chunks.
    """
    if not text.strip():
        return []

    if strategy == "sentence":
        return _chunk_sentences(text, max_chunk_size)
    elif strategy == "paragraph":
        return _chunk_paragraphs(text, max_chunk_size)
    elif strategy == "fixed":
        return _chunk_fixed(text, max_chunk_size, overlap)
    else:
        raise ValueError(f"Unknown chunking strategy: {strategy}")


def _chunk_sentences(text: str, max_size: int) -> list[str]:
    """Split on sentence boundaries, merging short sentences up to max_size."""
    # Split on sentence-ending punctuation followed by whitespace
    sentences = re.split(r"(?<=[.!?])\s+", text.strip())
    chunks: list[str] = []
    current = ""

    for sentence in sentences:
        sentence = sentence.strip()
        if not sentence:
            continue

        # If a single sentence exceeds max_size, split it with fixed-size chunking
        if len(sentence) > max_size:
            if current.strip():
                chunks.append(current.strip())
                current = ""
            chunks.extend(_chunk_fixed(sentence, max_size, overlap=50))
            continue

        if current and len(current) + len(sentence) + 1 > max_size:
            chunks.append(current.strip())
            current = sentence
        else:
            current = f"{current} {sentence}".strip() if current else sentence

    if current.strip():
        chunks.append(current.strip())

    return [c for c in chunks if c]


def _chunk_paragraphs(text: str, max_size: int) -> list[str]:
    """Split on double newlines, merging short paragraphs up to max_size."""
    paragraphs = re.split(r"\n\s*\n", text.strip())
    chunks: list[str] = []
    current = ""

    for para in paragraphs:
        para = para.strip()
        if not para:
            continue

        # If a single paragraph exceeds max_size, split it with fixed-size chunking
        if len(para) > max_size:
            if current.strip():
                chunks.append(current.strip())
                current = ""
            chunks.extend(_chunk_fixed(para, max_size, overlap=50))
            continue

        if current and len(current) + len(para) + 2 > max_size:
            chunks.append(current.strip())
            current = para
        else:
            current = f"{current}\n\n{para}".strip() if current else para

    if current.strip():
        chunks.append(current.strip())

    return [c for c in chunks if c]


def _chunk_fixed(text: str, max_size: int, overlap: int) -> list[str]:
    """Fixed-size chunks with overlap."""
    if len(text) <= max_size:
        return [text.strip()] if text.strip() else []

    chunks: list[str] = []
    start = 0
    step = max(1, max_size - overlap)

    while start < len(text):
        end = min(start + max_size, len(text))
        chunk = text[start:end].strip()
        if chunk:
            chunks.append(chunk)
        start += step

    return chunks
