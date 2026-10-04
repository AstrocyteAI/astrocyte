"""Where a local Astrocyte install keeps its config and data.

One definition shared by ``astrocyte-mcp``, ``astrocyte setup`` and
``astrocyte doctor``, so the server, the installer and the checker can never
disagree about which file is "the" config.
"""

from __future__ import annotations

import os
from pathlib import Path

CONFIG_ENV = "ASTROCYTE_CONFIG"


def config_path() -> Path:
    """``$ASTROCYTE_CONFIG``, else ``$XDG_CONFIG_HOME/astrocyte/astrocyte.yaml``
    (``~/.config/astrocyte/astrocyte.yaml``)."""
    if explicit := os.environ.get(CONFIG_ENV):
        return Path(explicit).expanduser()
    base = os.environ.get("XDG_CONFIG_HOME") or "~/.config"
    return Path(base).expanduser() / "astrocyte" / "astrocyte.yaml"


def data_dir() -> Path:
    """``$XDG_DATA_HOME/astrocyte`` (``~/.local/share/astrocyte``)."""
    base = os.environ.get("XDG_DATA_HOME") or "~/.local/share"
    return Path(base).expanduser() / "astrocyte"


def database_path() -> Path:
    return data_dir() / "astrocyte.db"


def state_dir() -> Path:
    """``$XDG_STATE_HOME/astrocyte`` (``~/.local/state/astrocyte``): the agent
    daemon's socket, lock, capture spool, per-session offsets and logs."""
    base = os.environ.get("XDG_STATE_HOME") or "~/.local/state"
    return Path(base).expanduser() / "astrocyte"


def agentd_endpoint() -> Path:
    """Where a TCP daemon (Windows) publishes its port and token."""
    return state_dir() / "agentd.json"


def agentd_socket() -> Path:
    # Kept short: AF_UNIX paths are capped at ~104 bytes on macOS.
    return state_dir() / "agentd.sock"


def spool_dir() -> Path:
    return state_dir() / "spool"
