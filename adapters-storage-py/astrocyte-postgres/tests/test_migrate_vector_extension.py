"""Tests for migrate.sh's VECTOR_EXTENSION env var handling.

These tests don't need a live Postgres — they run migrate.sh with a
stub ``psql`` on PATH that echoes its args to a file, then assert the
captured arguments contain the expected ``-v`` substitutions for each
``VECTOR_EXTENSION`` value.

Why this matters: the only way to silently produce a wrong-backend
schema (e.g. asking for pgvectorscale but ending up with HNSW) is for
migrate.sh's case statement to fall through. Pinning the allowlist
behavior here catches that regression at unit-test time.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
MIGRATE_SH = REPO_ROOT / "scripts" / "migrate.sh"


@pytest.fixture
def stub_psql(tmp_path: Path) -> tuple[Path, Path]:
    """Install a stub ``psql`` on PATH that logs every invocation.

    Returns ``(stub_dir, log_file)``: the directory to prepend to
    ``PATH`` and the log file each invocation appends to.
    """
    log = tmp_path / "psql.log"
    stub = tmp_path / "psql"
    stub.write_text(
        textwrap.dedent(
            f"""\
            #!/usr/bin/env bash
            # Append every invocation's args to the log, one per line,
            # then exit success so migrate.sh continues.
            printf '%s\\n' "$*" >> {log!s}
            exit 0
            """
        )
    )
    stub.chmod(stub.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return tmp_path, log


def _run_migrate(stub_dir: Path, env_overrides: dict[str, str]) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["PATH"] = f"{stub_dir}{os.pathsep}{env.get('PATH', '')}"
    env["DATABASE_URL"] = "postgresql://stub:stub@127.0.0.1:1/stub"
    env.update(env_overrides)
    return subprocess.run(
        ["bash", str(MIGRATE_SH)],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


class TestVectorExtensionDispatch:
    def test_default_is_pgvectorscale_diskann(self, stub_psql):
        """The default backend is pgvectorscale (DiskANN).

        Switched from pgvector (HNSW) on 2026-05-06 after the LME bench
        observed HNSW per-page write-lock drift under concurrent
        retains. pgvectorscale is OSS under the PostgreSQL License
        with no feature gates, so making it the default has no
        licensing trade-off — only an install-binary requirement that
        the shipped Dockerfile satisfies.
        """
        stub_dir, log = stub_psql
        result = _run_migrate(stub_dir, {})
        assert result.returncode == 0, result.stderr
        contents = log.read_text()
        # Default path passes the DiskANN USING clause and sets
        # install_vectorscale=1 so 001_extension.sql will fire
        # ``CREATE EXTENSION vectorscale``.
        assert "USING diskann (embedding vector_cosine_ops) WITH (num_neighbors = 50)" in contents
        assert "install_vectorscale=1" in contents
        # And explicitly NOT the HNSW path — silent fallback to HNSW
        # would mask install failures on systems without the
        # pgvectorscale binary.
        assert "USING hnsw" not in contents
        assert "vector_extension=pgvectorscale" in result.stdout

    def test_pgvector_explicit_fallback(self, stub_psql):
        """Explicit pgvector (HNSW) opt-in for environments without
        pgvectorscale binaries (vanilla Postgres images, restricted
        builds)."""
        stub_dir, log = stub_psql
        result = _run_migrate(stub_dir, {"VECTOR_EXTENSION": "pgvector"})
        assert result.returncode == 0, result.stderr
        contents = log.read_text()
        assert "USING hnsw" in contents
        assert "install_vectorscale" not in contents
        assert "USING diskann" not in contents

    def test_pgvectorscale_uses_diskann(self, stub_psql):
        stub_dir, log = stub_psql
        result = _run_migrate(stub_dir, {"VECTOR_EXTENSION": "pgvectorscale"})
        assert result.returncode == 0, result.stderr
        contents = log.read_text()
        # DiskANN USING clause and the install flag must both fire.
        assert "USING diskann (embedding vector_cosine_ops) WITH (num_neighbors = 50)" in contents
        assert "install_vectorscale=1" in contents
        # And the legacy HNSW clause MUST NOT be present — otherwise
        # the user's choice is silently ignored on existing DBs.
        assert "USING hnsw" not in contents
        assert "vector_extension=pgvectorscale" in result.stdout

    def test_vchord_uses_vchordrq(self, stub_psql):
        stub_dir, log = stub_psql
        result = _run_migrate(stub_dir, {"VECTOR_EXTENSION": "vchord"})
        assert result.returncode == 0, result.stderr
        contents = log.read_text()
        # vchordrq USING clause uses cosine ops (same operator class as
        # pgvector + pgvectorscale, so application ``<=>`` queries work
        # unchanged) and the install flag must fire.
        assert "USING vchordrq (embedding vector_cosine_ops)" in contents
        assert "install_vchord=1" in contents
        # Mutual exclusion: when vchord is selected the other backends'
        # USING clauses MUST NOT appear in the captured args.
        assert "USING hnsw" not in contents
        assert "USING diskann" not in contents
        assert "install_vectorscale" not in contents
        assert "vector_extension=vchord" in result.stdout

    def test_invalid_extension_rejected(self, stub_psql):
        stub_dir, _log = stub_psql
        result = _run_migrate(stub_dir, {"VECTOR_EXTENSION": "ivfflat"})
        # Allowlist must reject; silent fallback to pgvector would
        # confuse operators who explicitly asked for something else.
        assert result.returncode != 0
        # Error message must enumerate the supported values so an
        # operator who typo'd can self-correct without reading source.
        assert "VECTOR_EXTENSION must be" in result.stderr
        assert "pgvector" in result.stderr
        assert "pgvectorscale" in result.stderr
        assert "vchord" in result.stderr

    def test_astrocyte_prefixed_env_var_also_works(self, stub_psql):
        stub_dir, log = stub_psql
        # Both ASTROCYTE_VECTOR_EXTENSION and VECTOR_EXTENSION are
        # accepted; the prefixed one wins (matches the EMBEDDING_DIMENSIONS
        # convention already in migrate.sh).
        result = _run_migrate(
            stub_dir,
            {"ASTROCYTE_VECTOR_EXTENSION": "pgvectorscale", "VECTOR_EXTENSION": "pgvector"},
        )
        assert result.returncode == 0, result.stderr
        contents = log.read_text()
        assert "USING diskann" in contents
        assert "install_vectorscale=1" in contents


class TestMigrateSyntax:
    def test_bash_syntax(self):
        # ``bash -n`` parses without executing — catches typos in the
        # case statement or quoting before any test boots a Postgres.
        assert shutil.which("bash"), "bash required to run migrate.sh tests"
        result = subprocess.run(
            ["bash", "-n", str(MIGRATE_SH)],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr
