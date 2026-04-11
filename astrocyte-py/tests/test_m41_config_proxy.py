"""M4.1 — config validation for type: proxy sources."""

from __future__ import annotations

import pytest

from astrocyte.config import AstrocyteConfig, SourceConfig, validate_astrocyte_config
from astrocyte.errors import ConfigError


def test_proxy_source_requires_url():
    cfg = AstrocyteConfig()
    cfg.sources = {
        "remote": SourceConfig(type="proxy", target_bank="b1"),
    }
    with pytest.raises(ConfigError, match="url"):
        validate_astrocyte_config(cfg)


def test_proxy_source_requires_target_bank():
    cfg = AstrocyteConfig()
    cfg.sources = {
        "remote": SourceConfig(type="proxy", url="http://x?q={query}"),
    }
    with pytest.raises(ConfigError, match="target_bank"):
        validate_astrocyte_config(cfg)
