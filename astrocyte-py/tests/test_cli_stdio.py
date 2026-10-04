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
