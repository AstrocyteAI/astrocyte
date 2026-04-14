"""Tests for the ``astrocyte mip lint`` and ``astrocyte mip explain`` CLI commands."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from astrocyte.cli import main

_VALID_MIP = textwrap.dedent("""\
    version: "1.0"

    banks:
      - id: "student-{student_id}"
        description: Per-student memory
      - id: ops-monitoring
        description: Pipeline ops

    rules:
      - name: pii-lockdown
        priority: 1
        override: true
        match:
          pii_detected: true
        action:
          bank: private-encrypted
          tags: [pii]
          retain_policy: redact_before_store

      - name: student-answer
        priority: 10
        match:
          all:
            - content_type: student_answer
            - metadata.student_id: present
        action:
          bank: "student-{metadata.student_id}"
          tags: ["{metadata.topic}"]
          pipeline:
            version: 1
            chunker:
              strategy: dialogue
              max_size: 400
            dedup:
              threshold: 0.92
              action: skip_chunk
""")


@pytest.fixture
def mip_path(tmp_path: Path) -> Path:
    p = tmp_path / "mip.yaml"
    p.write_text(_VALID_MIP)
    return p


# ---------------------------------------------------------------------------
# lint
# ---------------------------------------------------------------------------


class TestMipLint:
    def test_lint_reports_ok_for_valid_config(self, mip_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        rc = main(["mip", "lint", str(mip_path)])
        out = capsys.readouterr().out
        assert rc == 0
        assert "ok:" in out
        assert "2 rule(s)" in out
        assert "2 bank(s)" in out

    def test_lint_returns_nonzero_for_missing_file(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        rc = main(["mip", "lint", str(tmp_path / "missing.yaml")])
        err = capsys.readouterr().err
        assert rc == 1
        assert "error" in err.lower()

    def test_lint_surfaces_loader_warnings(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        # An unknown sub-key in the pipeline block should trigger a warning.
        yaml_with_unknown = textwrap.dedent("""\
            version: "1.0"
            rules:
              - name: r1
                priority: 10
                match:
                  content_type: text
                action:
                  bank: b1
                  pipeline:
                    version: 1
                    chunker:
                      strategy: sentence
                      bogus_key: 42
        """)
        p = tmp_path / "mip.yaml"
        p.write_text(yaml_with_unknown)
        rc = main(["mip", "lint", str(p)])
        out = capsys.readouterr().out
        assert rc == 0
        assert "warning" in out.lower()
        assert "bogus_key" in out


# ---------------------------------------------------------------------------
# explain
# ---------------------------------------------------------------------------


class TestMipExplain:
    def test_explain_picks_pii_override_rule(self, mip_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        rc = main(
            [
                "mip", "explain", str(mip_path),
                "--content", "irrelevant body",
                "--content-type", "text",
                "--pii-detected",
            ],
        )
        out = capsys.readouterr().out
        assert rc == 0
        assert "pii-lockdown" in out
        assert "private-encrypted" in out  # bank from override rule
        assert "redact_before_store" in out

    def test_explain_picks_student_rule_and_renders_pipeline(self, mip_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        rc = main(
            [
                "mip", "explain", str(mip_path),
                "--content", "an answer",
                "--content-type", "student_answer",
                "--metadata", "student_id=42",
                "--metadata", "topic=algebra",
            ],
        )
        out = capsys.readouterr().out
        assert rc == 0
        assert "student-answer" in out
        assert "student-42" in out  # template interpolation worked
        assert "algebra" in out
        # Pipeline section is rendered
        assert "pipeline:" in out
        assert "chunker" in out
        assert "dialogue" in out
        assert "version: 1" in out

    def test_explain_with_no_match_returns_zero_and_says_so(self, mip_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        rc = main(
            [
                "mip", "explain", str(mip_path),
                "--content", "random",
                "--content-type", "weird_unknown_type",
            ],
        )
        out = capsys.readouterr().out
        assert rc == 0
        assert "Matched rules (0)" in out

    def test_explain_rejects_malformed_metadata(self, mip_path: Path) -> None:
        with pytest.raises(SystemExit):
            main(
                [
                    "mip", "explain", str(mip_path),
                    "--content", "x",
                    "--content-type", "text",
                    "--metadata", "no_equals_sign",
                ],
            )


# ---------------------------------------------------------------------------
# Forget guardrails surfaced through CLI (Phase 4 / item 2a)
# ---------------------------------------------------------------------------


_FORGET_INVALID_MIP = textwrap.dedent("""\
    version: "1.0"
    rules:
      - name: bad-forget
        priority: 10
        match:
          content_type: pii
        action:
          bank: vault
          forget:
            version: 1
            mode: hard
            # missing audit: required → discipline rule should fire
""")

_FORGET_VALID_MIP = textwrap.dedent("""\
    version: "1.0"
    rules:
      - name: gdpr-erasure
        priority: 1
        match:
          content_type: pii
        action:
          bank: vault
          forget:
            version: 1
            preset: gdpr
            max_per_call: 50
""")


class TestCliForgetGuardrails:
    def test_lint_rejects_hard_mode_without_audit_required(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str],
    ) -> None:
        path = tmp_path / "mip.yaml"
        path.write_text(_FORGET_INVALID_MIP)
        rc = main(["mip", "lint", str(path)])
        assert rc == 1
        err = capsys.readouterr().err
        assert "hard" in err.lower()
        assert "audit" in err.lower()

    def test_lint_accepts_gdpr_preset_forget_block(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str],
    ) -> None:
        path = tmp_path / "mip.yaml"
        path.write_text(_FORGET_VALID_MIP)
        rc = main(["mip", "lint", str(path)])
        assert rc == 0
        out = capsys.readouterr().out
        assert "ok:" in out

    def test_sample_code_preset_fixture_lints_clean(
        self, capsys: pytest.CaptureFixture[str],
    ) -> None:
        """examples/mip-code.yaml is the canonical sample for the `code` preset
        and a forget-aware MIP config — it must always lint clean so it can be
        copy-pasted by users without further edits."""
        fixture = (
            Path(__file__).resolve().parent.parent / "examples" / "mip-code.yaml"
        )
        rc = main(["mip", "lint", str(fixture)])
        out = capsys.readouterr().out
        assert rc == 0, out
        assert "ok:" in out
        assert "4 rule(s)" in out
        assert "2 bank(s)" in out

    def test_explain_renders_forget_block(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str],
    ) -> None:
        path = tmp_path / "mip.yaml"
        path.write_text(_FORGET_VALID_MIP)
        rc = main([
            "mip", "explain", str(path),
            "--content", "ssn 123",
            "--content-type", "pii",
        ])
        out = capsys.readouterr().out
        assert rc == 0
        assert "forget:" in out
        assert "mode: hard" in out
        assert "audit: required" in out
        assert "max_per_call: 50" in out
