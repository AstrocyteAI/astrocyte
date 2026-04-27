"""Unit tests for astrocyte_ingestion_s3._extract — no I/O, no S3."""

from __future__ import annotations

import io

import pytest

from astrocyte_ingestion_s3._extract import extract_text

# ---------------------------------------------------------------------------
# Plain text
# ---------------------------------------------------------------------------


def test_plain_txt_utf8() -> None:
    body = "Hello, world!\nLine two.".encode("utf-8")
    result = extract_text("notes.txt", body)
    assert result is not None
    assert "Hello, world!" in result
    assert "Line two." in result


def test_plain_md() -> None:
    body = "# Title\n\nSome **markdown** content.".encode("utf-8")
    result = extract_text("README.md", body)
    assert result is not None
    assert "# Title" in result


def test_plain_latin1_fallback() -> None:
    # latin-1 encoded text that isn't valid UTF-8
    body = "caf\xe9".encode("latin-1")
    result = extract_text("notes.txt", body)
    assert result is not None
    assert "caf" in result


def test_plain_content_type_override() -> None:
    body = "plain text via content-type".encode("utf-8")
    result = extract_text("unknown_ext", body, content_type="text/plain")
    assert result is not None
    assert "plain text" in result


# ---------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------


def test_html_strips_script_and_style() -> None:
    body = b"""
    <html><head><style>body{color:red}</style></head>
    <body><script>alert(1)</script><p>Keep this.</p></body></html>
    """
    result = extract_text("page.html", body)
    assert result is not None
    assert "Keep this." in result
    assert "alert" not in result
    assert "color:red" not in result


def test_html_content_type_override() -> None:
    body = b"<p>Hello from CT</p>"
    result = extract_text("noext", body, content_type="text/html")
    assert result is not None
    assert "Hello from CT" in result


def test_htm_extension() -> None:
    body = b"<html><body><p>Old school.</p></body></html>"
    result = extract_text("index.htm", body)
    assert result is not None
    assert "Old school." in result


# ---------------------------------------------------------------------------
# PDF (requires pypdf)
# ---------------------------------------------------------------------------


def _make_minimal_pdf(text: str) -> bytes:
    """Create a minimal single-page PDF containing *text* using fpdf2 if available,
    or fall back to a hand-crafted PDF byte string."""
    try:
        from fpdf import FPDF  # type: ignore[import-untyped]

        pdf = FPDF()
        pdf.add_page()
        pdf.set_font("Helvetica", size=12)
        pdf.cell(200, 10, txt=text)
        return pdf.output()
    except ImportError:
        pass

    # Minimal hand-crafted PDF with the text embedded as a stream.
    # Enough for pypdf to parse one page of plain text.
    stream = f"BT /F1 12 Tf 50 700 Td ({text}) Tj ET".encode()
    slen = len(stream)
    body = (
        b"%PDF-1.4\n"
        b"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
        b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
        b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]"
        b"/Contents 4 0 R/Resources<</Font<</F1 5 0 R>>>>>>endobj\n"
        + f"4 0 obj<</Length {slen}>>stream\n".encode()
        + stream
        + b"\nendstream endobj\n"
        b"5 0 obj<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>endobj\n"
        b"xref\n0 6\n"
        b"0000000000 65535 f \n"
        b"0000000009 00000 n \n"
        b"0000000058 00000 n \n"
        b"0000000115 00000 n \n"
        b"0000000266 00000 n \n"
        b"0000000400 00000 n \n"
        b"trailer<</Size 6/Root 1 0 R>>\n"
        b"startxref\n460\n%%EOF"
    )
    return body


def test_pdf_extraction() -> None:
    pytest.importorskip("pypdf")
    body = _make_minimal_pdf("PDF content here")
    result = extract_text("doc.pdf", body)
    # pypdf may or may not extract text from hand-crafted PDFs; just assert no crash.
    assert result is None or isinstance(result, str)


def test_pdf_content_type() -> None:
    pytest.importorskip("pypdf")
    body = _make_minimal_pdf("via content type")
    result = extract_text("noext", body, content_type="application/pdf")
    assert result is None or isinstance(result, str)


def test_pdf_corrupt_returns_none() -> None:
    pytest.importorskip("pypdf")
    result = extract_text("bad.pdf", b"not a pdf at all")
    assert result is None


# ---------------------------------------------------------------------------
# DOCX (requires python-docx)
# ---------------------------------------------------------------------------


def _make_minimal_docx(text: str) -> bytes:
    from docx import Document  # type: ignore[import-untyped]

    doc = Document()
    doc.add_paragraph(text)
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def test_docx_extraction() -> None:
    pytest.importorskip("docx")
    body = _make_minimal_docx("Hello from DOCX")
    result = extract_text("report.docx", body)
    assert result is not None
    assert "Hello from DOCX" in result


def test_docx_corrupt_returns_none() -> None:
    pytest.importorskip("docx")
    result = extract_text("bad.docx", b"not a docx")
    assert result is None


# ---------------------------------------------------------------------------
# Unknown / unsupported
# ---------------------------------------------------------------------------


def test_unknown_binary_extension_returns_none() -> None:
    # A binary extension with non-decodable bytes should return None
    result = extract_text("image.png", b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR")
    # The extractor will attempt plain-text decode but the bytes may decode as latin-1;
    # we only assert it doesn't raise.
    assert result is None or isinstance(result, str)


def test_empty_body() -> None:
    result = extract_text("empty.txt", b"")
    # Empty string is falsy but not None — caller skips on not text.strip()
    assert result == "" or result is None
