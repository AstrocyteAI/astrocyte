"""The CLI speaks UTF-8 on Windows, whatever the console code page."""

from __future__ import annotations

import io
import sys

from astrocyte import cli


def _cp1252_stream() -> io.TextIOWrapper:
    return io.TextIOWrapper(io.BytesIO(), encoding="cp1252")


def test_windows_output_is_utf8_so_status_marks_do_not_crash(monkeypatch):
    out, err, stdin = _cp1252_stream(), _cp1252_stream(), _cp1252_stream()
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", err)
    monkeypatch.setattr(sys, "stdin", stdin)
    cli._utf8_stdio(windows=True)
    print("  ✓ server       starts and answers")
    out.flush()
    assert out.buffer.getvalue().decode("utf-8").strip().startswith("✓ server")
    assert err.encoding == "utf-8" and stdin.encoding == "utf-8", "hook payloads arrive as UTF-8 too"


def test_elsewhere_the_streams_are_left_alone(monkeypatch):
    out = _cp1252_stream()
    monkeypatch.setattr(sys, "stdout", out)
    cli._utf8_stdio(windows=False)
    assert out.encoding == "cp1252"


def test_streams_without_reconfigure_are_skipped(monkeypatch):
    monkeypatch.setattr(sys, "stdout", io.StringIO())
    cli._utf8_stdio(windows=True)  # no AttributeError


def test_config_files_saved_by_windows_editors_still_load(tmp_path):
    """UTF-8 with a BOM (some Windows editors), and the legacy code page
    (configs written before reads were UTF-8, or by an editor that still uses it)."""
    import locale

    from astrocyte._text_files import read_user_text

    bom = tmp_path / "bom.yaml"
    bom.write_bytes("﻿llm_provider: mock  # — dash\n".encode())
    assert read_user_text(bom) == "llm_provider: mock  # — dash\n"
    legacy = tmp_path / "legacy.yaml"
    legacy.write_bytes(b"llm_provider: mock  # \x97 cp1252 dash\n")
    text = read_user_text(legacy)
    assert text.startswith("llm_provider: mock") and "cp1252 dash" in text
    assert locale.getpreferredencoding(False)  # the fallback has an encoding to use


def test_load_config_reads_a_utf8_config_with_a_bom(tmp_path):
    from astrocyte.config import load_config

    path = tmp_path / "astrocyte.yaml"
    path.write_bytes("﻿llm_provider: mock  # — généré\nvector_store: in_memory\n".encode())
    assert load_config(str(path)).llm_provider == "mock"
