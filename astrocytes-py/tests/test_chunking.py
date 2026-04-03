"""Tests for pipeline/chunking.py — text splitting strategies."""

import pytest

from astrocytes.pipeline.chunking import chunk_text


class TestSentenceChunking:
    def test_single_sentence(self):
        chunks = chunk_text("Hello world.", strategy="sentence")
        assert len(chunks) == 1
        assert chunks[0] == "Hello world."

    def test_multiple_sentences(self):
        text = "First sentence. Second sentence. Third sentence."
        chunks = chunk_text(text, strategy="sentence", max_chunk_size=40)
        assert len(chunks) >= 2

    def test_merges_short_sentences(self):
        text = "Hi. Yes. No. OK."
        chunks = chunk_text(text, strategy="sentence", max_chunk_size=100)
        assert len(chunks) == 1  # All fit in one chunk

    def test_empty_text(self):
        assert chunk_text("", strategy="sentence") == []

    def test_whitespace_only(self):
        assert chunk_text("   \n\t  ", strategy="sentence") == []


class TestParagraphChunking:
    def test_single_paragraph(self):
        chunks = chunk_text("Hello world", strategy="paragraph")
        assert len(chunks) == 1

    def test_multiple_paragraphs(self):
        text = "First paragraph.\n\nSecond paragraph.\n\nThird paragraph."
        chunks = chunk_text(text, strategy="paragraph", max_chunk_size=30)
        assert len(chunks) >= 2

    def test_merges_short_paragraphs(self):
        text = "A.\n\nB.\n\nC."
        chunks = chunk_text(text, strategy="paragraph", max_chunk_size=100)
        assert len(chunks) == 1


class TestFixedChunking:
    def test_short_text(self):
        chunks = chunk_text("hello", strategy="fixed", max_chunk_size=100)
        assert len(chunks) == 1

    def test_splits_long_text(self):
        text = "a" * 200
        chunks = chunk_text(text, strategy="fixed", max_chunk_size=100, overlap=0)
        assert len(chunks) == 2

    def test_overlap(self):
        text = "a" * 200
        chunks = chunk_text(text, strategy="fixed", max_chunk_size=100, overlap=50)
        assert len(chunks) >= 3  # More chunks due to overlap

    def test_unknown_strategy(self):
        with pytest.raises(ValueError, match="Unknown"):
            chunk_text("hello", strategy="invalid")
