"""Agents the user switched off, remembered across ``astrocyte setup`` runs.

Plain ``astrocyte setup`` wires every agent it detects. Without a record of
what the user turned off, the next plain run (after an upgrade, say) turns it
back on. ``astrocyte uninstall --<agent>`` and ``astrocyte setup --no-hooks``
record the choice here; naming the agent again (``astrocyte setup --claude``)
clears it.

Kept beside the config file, as ``harnesses.json``: it is a preference, and a
different config (``--config`` / ``$ASTROCYTE_CONFIG``) is a different install.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

FILE_NAME = "harnesses.json"


@dataclass
class Choices:
    off: set[str] = field(default_factory=set)  # not wired at all
    hooks_off: set[str] = field(default_factory=set)  # wired, without automatic memory
    file_recall: set[str] = field(default_factory=set)  # opted in to recall on file reads/edits


def choices_path(config: Path) -> Path:
    return config.parent / FILE_NAME


def load_choices(config: Path) -> Choices:
    """The recorded choices; none when the file is missing or unreadable."""
    try:
        data = json.loads(choices_path(config).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return Choices()
    if not isinstance(data, dict):
        return Choices()

    def keys(name: str) -> set[str]:
        value = data.get(name)
        return {k for k in value if isinstance(k, str)} if isinstance(value, list) else set()

    return Choices(off=keys("off"), hooks_off=keys("hooks_off"), file_recall=keys("file_recall"))


def save_choices(config: Path, choices: Choices) -> None:
    path = choices_path(config)
    if not (choices.off or choices.hooks_off or choices.file_recall or path.exists()):
        return  # no choice made: no file to leave behind
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    data = {"off": sorted(choices.off), "hooks_off": sorted(choices.hooks_off)}
    if choices.file_recall:
        data["file_recall"] = sorted(choices.file_recall)
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)
