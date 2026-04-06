"""Tests for MIP config loader."""

import os
from pathlib import Path

import pytest

from astrocyte.errors import ConfigError
from astrocyte.mip.loader import load_mip_config


@pytest.fixture
def mip_yaml(tmp_path: Path) -> Path:
    content = """
version: "1.0"

banks:
  - id: "student-{student_id}"
    description: Per-student memory
    access: ["agent:tutor", "agent:grader"]
    compliance: pdpa

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
      tags:
        - "{metadata.topic}"

intent_policy:
  model_context: "Route content. Banks: {banks}. Tags: {tags}."
  constraints:
    cannot_override: [pii-lockdown]
    must_justify: true
    max_tokens: 200
"""
    p = tmp_path / "mip.yaml"
    p.write_text(content)
    return p


class TestLoadMipConfig:
    def test_load_valid(self, mip_yaml: Path) -> None:
        config = load_mip_config(mip_yaml)
        assert config.version == "1.0"
        assert len(config.banks) == 1
        assert config.banks[0].id == "student-{student_id}"
        assert config.banks[0].compliance == "pdpa"
        assert len(config.rules) == 2
        assert config.rules[0].name == "pii-lockdown"
        assert config.rules[0].override is True
        assert config.rules[1].name == "student-answer"
        assert config.intent_policy is not None
        assert config.intent_policy.model_context is not None

    def test_missing_file_raises(self) -> None:
        with pytest.raises(ConfigError, match="not found"):
            load_mip_config("/nonexistent/mip.yaml")

    def test_env_substitution(self, tmp_path: Path) -> None:
        content = """
version: "1.0"
rules:
  - name: env-test
    priority: 1
    match:
      source: "${MIP_TEST_SOURCE}"
    action:
      bank: "${MIP_TEST_BANK}"
"""
        p = tmp_path / "mip-env.yaml"
        p.write_text(content)

        os.environ["MIP_TEST_SOURCE"] = "test-agent"
        os.environ["MIP_TEST_BANK"] = "test-bank"
        try:
            config = load_mip_config(p)
            assert config.rules[0].match.all_conditions[0].value == "test-agent"
            assert config.rules[0].action.bank == "test-bank"
        finally:
            del os.environ["MIP_TEST_SOURCE"]
            del os.environ["MIP_TEST_BANK"]

    def test_duplicate_rule_names_raises(self, tmp_path: Path) -> None:
        content = """
rules:
  - name: duplicate
    priority: 1
    match:
      content_type: a
    action:
      bank: a
  - name: duplicate
    priority: 2
    match:
      content_type: b
    action:
      bank: b
"""
        p = tmp_path / "bad.yaml"
        p.write_text(content)
        with pytest.raises(ConfigError, match="Duplicate rule names"):
            load_mip_config(p)

    def test_override_with_escalate_raises(self, tmp_path: Path) -> None:
        content = """
rules:
  - name: bad-rule
    priority: 1
    override: true
    match:
      content_type: test
    action:
      escalate: mip
"""
        p = tmp_path / "bad2.yaml"
        p.write_text(content)
        with pytest.raises(ConfigError, match="override=true and escalate=mip"):
            load_mip_config(p)
