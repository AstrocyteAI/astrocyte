"""Validation for ``sources:`` poll / GitHub (M4 api_poll)."""

from __future__ import annotations

import pytest

from astrocyte.config import AstrocyteConfig, SourceConfig, validate_astrocyte_config
from astrocyte.errors import ConfigError


def test_validate_poll_github_ok() -> None:
    c = AstrocyteConfig()
    c.sources = {
        "gh": SourceConfig(
            type="poll",
            driver="github",
            path="myorg/myrepo",
            interval_seconds=60,
            target_bank="b1",
            auth={"token": "fake"},
        ),
    }
    validate_astrocyte_config(c)


def test_validate_poll_github_alias_api_poll() -> None:
    c = AstrocyteConfig()
    c.sources = {
        "gh": SourceConfig(
            type="api_poll",
            driver="github",
            path="myorg/myrepo",
            interval_seconds=60,
            target_bank="b1",
            auth={"token": "x"},
        ),
    }
    validate_astrocyte_config(c)


@pytest.mark.parametrize(
    ("src", "msg_part"),
    [
        (
            SourceConfig(
                type="poll",
                driver="gitlab",
                path="a/b",
                interval_seconds=60,
                target_bank="b1",
                auth={"token": "x"},
            ),
            "not supported",
        ),
        (
            SourceConfig(
                type="poll",
                driver="github",
                path="bad",
                interval_seconds=60,
                target_bank="b1",
                auth={"token": "x"},
            ),
            "owner/repo",
        ),
        (
            SourceConfig(
                type="poll",
                driver="github",
                path="a/b",
                interval_seconds=5,
                target_bank="b1",
                auth={"token": "x"},
            ),
            "interval_seconds",
        ),
        (
            SourceConfig(
                type="poll",
                driver="github",
                path="a/b",
                interval_seconds=60,
                auth={"token": "x"},
            ),
            "target_bank",
        ),
        (
            SourceConfig(
                type="poll",
                driver="github",
                path="a/b",
                interval_seconds=60,
                target_bank="b1",
            ),
            "auth.token",
        ),
    ],
)
def test_validate_poll_github_errors(src: SourceConfig, msg_part: str) -> None:
    c = AstrocyteConfig()
    c.sources = {"x": src}
    with pytest.raises(ConfigError, match=msg_part):
        validate_astrocyte_config(c)
