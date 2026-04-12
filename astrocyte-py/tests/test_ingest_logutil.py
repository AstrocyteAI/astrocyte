"""Structured ingest logging (``astrocyte.ingest.logutil``)."""

from __future__ import annotations

import json
import logging

import pytest

from astrocyte.ingest.logutil import log_ingest_event


def test_log_ingest_event_plain(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    monkeypatch.delenv("ASTROCYTE_LOG_FORMAT", raising=False)
    log = logging.getLogger("test.ingest")
    with caplog.at_level(logging.INFO, logger="test.ingest"):
        log_ingest_event(log, "demo_event", source_id="s1", n=2)
    assert "demo_event" in caplog.text
    assert "s1" in caplog.text


def test_log_ingest_event_json(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    monkeypatch.setenv("ASTROCYTE_LOG_FORMAT", "json")
    log = logging.getLogger("test.ingest.json")
    with caplog.at_level(logging.INFO, logger="test.ingest.json"):
        log_ingest_event(log, "demo_event", source_id="s1", n=2)
    line = caplog.records[0].getMessage()
    data = json.loads(line)
    assert data["event"] == "demo_event"
    assert data["source_id"] == "s1"
    assert data["n"] == 2
