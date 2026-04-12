"""Text chunking — split content into memory-sized pieces.

Sync, pure computation — Rust migration candidate.
See docs/_design/built-in-pipeline.md section 2.
"""

from __future__ import annotations

import re

#: Default maximum characters per chunk.
DEFAULT_CHUNK_SIZE = 512

#: Default character overlap between consecutive chunks.
DEFAULT_CHUNK_OVERLAP = 50


def chunk_text(
    text: str,
    strategy: str = "sentence",
    max_chunk_size: int = DEFAULT_CHUNK_SIZE,
    overlap: int = DEFAULT_CHUNK_OVERLAP,
) -> list[str]:
    """Split text into chunks using the specified strategy.

    Strategies:
        - "sentence": split on sentence boundaries (.!?)
        - "paragraph": split on double newlines
        - "fixed": fixed character count with overlap
        - "dialogue": split on speaker turn boundaries (``speaker: text`` format)

    Returns list of non-empty chunks.
    """
    if not text.strip():
        return []

    if strategy == "sentence":
        return _chunk_sentences(text, max_chunk_size, overlap)
    elif strategy == "paragraph":
        return _chunk_paragraphs(text, max_chunk_size, overlap)
    elif strategy == "dialogue":
        return _chunk_dialogue(text, max_chunk_size, overlap)
    elif strategy == "fixed":
        return _chunk_fixed(text, max_chunk_size, overlap)
    else:
        raise ValueError(f"Unknown chunking strategy: {strategy}")


def _chunk_sentences(text: str, max_size: int, overlap: int) -> list[str]:
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
            chunks.extend(_chunk_fixed(sentence, max_size, overlap=overlap))
            continue

        if current and len(current) + len(sentence) + 1 > max_size:
            chunks.append(current.strip())
            current = sentence
        else:
            current = f"{current} {sentence}".strip() if current else sentence

    if current.strip():
        chunks.append(current.strip())

    return [c for c in chunks if c]


def _chunk_paragraphs(text: str, max_size: int, overlap: int) -> list[str]:
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
            chunks.extend(_chunk_fixed(para, max_size, overlap=overlap))
            continue

        if current and len(current) + len(para) + 2 > max_size:
            chunks.append(current.strip())
            current = para
        else:
            current = f"{current}\n\n{para}".strip() if current else para

    if current.strip():
        chunks.append(current.strip())

    return [c for c in chunks if c]


def _chunk_dialogue(text: str, max_size: int, overlap: int) -> list[str]:
    """Split on speaker turn boundaries, keeping complete turns together.

    Expects the ``speaker: text`` format (one turn per line). Groups consecutive
    turns into chunks up to ``max_size`` without splitting a turn across chunks.
    Falls back to sentence chunking for turns that exceed ``max_size``.
    """
    # Split into individual turns at line boundaries where a speaker label starts
    lines = text.strip().split("\n")
    turns: list[str] = []
    current_turn = ""

    for line in lines:
        line = line.rstrip()
        if not line:
            continue
        # New turn starts when line matches "word(s): text" pattern
        if re.match(r"^[A-Za-z][\w\s]*:", line) and current_turn:
            turns.append(current_turn.strip())
            current_turn = line
        else:
            # Continuation of current turn (or first line)
            current_turn = f"{current_turn}\n{line}" if current_turn else line

    if current_turn.strip():
        turns.append(current_turn.strip())

    if not turns:
        return _chunk_sentences(text, max_size, overlap)

    # Group turns into chunks up to max_size
    chunks: list[str] = []
    current = ""

    for turn in turns:
        # If a single turn exceeds max_size, split it but keep speaker label
        if len(turn) > max_size:
            if current.strip():
                chunks.append(current.strip())
                current = ""
            # Extract speaker label and split the rest
            match = re.match(r"^([A-Za-z][\w\s]*:)\s*", turn)
            if match:
                speaker_prefix = match.group(1) + " "
                turn_text = turn[match.end() :]
                sub_chunks = _chunk_sentences(turn_text, max_size - len(speaker_prefix), overlap)
                chunks.extend(f"{speaker_prefix}{sc}" for sc in sub_chunks)
            else:
                chunks.extend(_chunk_sentences(turn, max_size, overlap))
            continue

        if current and len(current) + len(turn) + 1 > max_size:
            chunks.append(current.strip())
            current = turn
        else:
            current = f"{current}\n{turn}" if current else turn

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
