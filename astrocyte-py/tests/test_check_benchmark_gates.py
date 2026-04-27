from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SCRIPT_PATH = _REPO_ROOT / "scripts" / "check_benchmark_gates.py"
_SPEC = importlib.util.spec_from_file_location("check_benchmark_gates", _SCRIPT_PATH)
assert _SPEC and _SPEC.loader
_CHECKER = importlib.util.module_from_spec(_SPEC)
sys.modules["check_benchmark_gates"] = _CHECKER
_SPEC.loader.exec_module(_CHECKER)


def _gates() -> dict:
    return {
        "locomo": {
            "minimums": {"overall_accuracy": 0.75, "metrics.recall_hit_rate": 0.80},
            "maximums": {"metrics.recall_latency_p95_ms": 2500},
        }
    }


def _passing_results() -> dict:
    return {
        "locomo": {
            "overall_accuracy": 0.80,
            "metrics": {
                "recall_hit_rate": 0.85,
                "recall_latency_p95_ms": 1200,
            },
        }
    }


def test_check_gates_passes_when_minimums_and_maximums_hold() -> None:
    failures, rows = _CHECKER.check_gates(_gates(), _passing_results())

    assert failures == []
    assert {row[3] for row in rows} == {"pass"}


def test_check_gates_fails_on_quality_drop() -> None:
    results = _passing_results()
    results["locomo"]["overall_accuracy"] = 0.70

    failures, _rows = _CHECKER.check_gates(_gates(), results)

    assert any("overall_accuracy" in failure for failure in failures)


def test_check_gates_fails_on_latency_over_budget() -> None:
    results = _passing_results()
    results["locomo"]["metrics"]["recall_latency_p95_ms"] = 3000

    failures, _rows = _CHECKER.check_gates(_gates(), results)

    assert any("recall_latency_p95_ms" in failure for failure in failures)


def test_main_returns_zero_for_passing_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    gates_path = tmp_path / "gates.json"
    results_path = tmp_path / "results.json"
    gates_path.write_text(json.dumps(_gates()), encoding="utf-8")
    results_path.write_text(json.dumps(_passing_results()), encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["check", "--gates", str(gates_path), "--results", str(results_path)])

    assert _CHECKER.main() == 0
