"""One memory bank per project, derived identically everywhere.

The hooks (which receive the session's ``cwd``) and the MCP server (launched
with the session's cwd as its own — measured on Claude Code 2.1) must land in
the *same* bank, or automatic capture and the agent's own ``memory_retain``
calls never meet. Both call :func:`project_bank` on that directory.

Identity prefers the git ``origin`` URL, so every clone and worktree of a repo
shares one memory; without a remote it falls back to the repo root path. A
directory outside any repo is its own project.
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
from pathlib import Path

_SLUG = re.compile(r"[^a-z0-9._-]+")


def _git(cwd: str, *args: str) -> str | None:
    try:
        out = subprocess.run(
            ["git", "-C", cwd, *args], capture_output=True, text=True, timeout=2, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() or None if out.returncode == 0 else None


def _normalise_remote(url: str) -> str:
    """``git@github.com:Org/Repo.git`` and ``https://github.com/org/repo`` → ``github.com/org/repo``."""
    u = url.strip().lower()
    u = re.sub(r"^[a-z+]+://", "", u)
    u = re.sub(r"^[^@/]+@", "", u)  # user@ in ssh / https-with-credentials
    u = u.replace(":", "/", 1) if "/" not in u.split(":", 1)[0] else u
    return u.rstrip("/").removesuffix(".git").rstrip("/")


def project_root(cwd: str) -> Path:
    root = _git(cwd, "rev-parse", "--show-toplevel")
    return Path(root) if root else Path(cwd).resolve()


def project_bank(cwd: str | None = None) -> str:
    cwd = cwd or os.getcwd()
    root = project_root(cwd)
    remote = _git(str(root), "config", "--get", "remote.origin.url")
    if remote:
        identity = _normalise_remote(remote)
        name = identity.rsplit("/", 1)[-1]
    else:
        identity = str(root)
        name = root.name
    slug = _SLUG.sub("-", name.lower()).strip("-.")[:40] or "project"
    digest = hashlib.sha1(identity.encode("utf-8")).hexdigest()[:8]
    return f"project:{slug}-{digest}"
