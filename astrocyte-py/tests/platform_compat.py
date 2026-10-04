"""What tests need to run the same on Linux, macOS and Windows."""

from __future__ import annotations

import os
import shutil
import stat
import sys
from pathlib import Path

import pytest

WINDOWS = os.name == "nt"

#: POSIX permission bits; Windows protects files with ACLs on the user profile.
posix_modes = pytest.mark.skipif(WINDOWS, reason="POSIX file modes; Windows relies on profile ACLs")


def system_env() -> dict[str, str]:
    """The variables a child process can't start without on Windows (asyncio
    import fails with WinError 10106 when SYSTEMROOT is missing). Nothing on POSIX."""
    if not WINDOWS:
        return {}
    return {k: os.environ[k] for k in ("SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "TEMP", "TMP")
            if k in os.environ}


def system_path() -> list[str]:
    """A minimal PATH: the system's own tools, plus git (tests make repos)."""
    if not WINDOWS:
        return ["/usr/bin", "/bin"]
    root = os.environ.get("SYSTEMROOT", r"C:\Windows")
    dirs = [os.path.join(root, "System32"), root]
    if git := shutil.which("git"):
        dirs.append(str(Path(git).parent))
    return dirs


def set_home(monkeypatch: pytest.MonkeyPatch, home: Path) -> None:
    """Point ``~`` at ``home``: $HOME on POSIX, USERPROFILE on Windows."""
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))


def write_executable(bin_dir: Path, name: str, source: str) -> Path:
    """A Python script runnable as ``name``. POSIX: a shebang file. Windows:
    ``name.cmd`` calling the script, which is how npm installs ``claude`` and
    ``codex`` there (and what ``shutil.which`` finds through PATHEXT).
    Returns the path to invoke."""
    body = source.split("\n", 1)[1] if source.startswith("#!") else source
    if WINDOWS:
        script = bin_dir / f"{name}.py"
        script.write_text(body, encoding="utf-8")
        launcher = bin_dir / f"{name}.cmd"
        launcher.write_text(f'@"{sys.executable}" "{script}" %*\r\n', encoding="utf-8")
        return launcher
    path = bin_dir / name
    path.write_text(f"#!{sys.executable}\n{body}", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path
