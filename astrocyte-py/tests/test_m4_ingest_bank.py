"""M4 — target bank resolution for ingest sources (TDD)."""

from __future__ import annotations

import pytest

from astrocyte.config import SourceConfig
from astrocyte.errors import IngestError
from astrocyte.ingest.bank_resolve import resolve_ingest_bank_id


class TestResolveIngestBankId:
    def test_target_bank_literal(self):
        cfg = SourceConfig(type="webhook", target_bank="bank-alpha")
        assert resolve_ingest_bank_id(cfg) == "bank-alpha"

    def test_template_substitutes_principal(self):
        cfg = SourceConfig(type="webhook", target_bank_template="user-{principal}")
        assert resolve_ingest_bank_id(cfg, principal="calvin") == "user-calvin"

    def test_template_requires_principal(self):
        cfg = SourceConfig(type="webhook", target_bank_template="user-{principal}")
        with pytest.raises(IngestError, match="principal"):
            resolve_ingest_bank_id(cfg, principal=None)

    def test_requires_target(self):
        cfg = SourceConfig(type="webhook")
        with pytest.raises(IngestError, match="target_bank"):
            resolve_ingest_bank_id(cfg)
