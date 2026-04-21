"""Regression-gate script tests (L4 eval harness).

Covers ``scripts/check_benchmark_regression.py`` — the tool that compares
a benchmark run against a checked-in baseline and fails CI when scores
drop beyond per-field tolerances.

The tests import the script module directly. We avoid subprocess so
failures produce readable tracebacks, not opaque exit codes.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

# Load the script module under an importable name. It's in /scripts/
# which isn't on sys.path, so we go through importlib.
_REPO_ROOT = Path(__file__).resolve().parent.parent
_SCRIPT_PATH = _REPO_ROOT / "scripts" / "check_benchmark_regression.py"
_SPEC = importlib.util.spec_from_file_location("check_benchmark_regression", _SCRIPT_PATH)
assert _SPEC and _SPEC.loader
_CHECKER = importlib.util.module_from_spec(_SPEC)
sys.modules["check_benchmark_regression"] = _CHECKER
_SPEC.loader.exec_module(_CHECKER)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _baseline() -> dict:
    return {
        "locomo": {
            "overall_accuracy": 0.60,
            "category_accuracy": {"multi-hop": 0.15, "temporal": 0.70},
            "metrics": {"recall_hit_rate": 0.55, "recall_mrr": 0.45, "recall_precision": 0.28},
        },
        "longmemeval": {
            "overall_accuracy": 0.20,
            "category_accuracy": {"reasoning": 0.27, "extraction": 0.17},
            "metrics": {"recall_hit_rate": 0.18, "recall_mrr": 0.07, "recall_precision": 0.03},
        },
    }


def _results_at_baseline() -> dict:
    """A results file with identical numbers to the baseline."""
    return {
        "locomo": {
            "overall_accuracy": 0.60,
            "category_accuracy": {"multi-hop": 0.15, "temporal": 0.70},
            "metrics": {"recall_hit_rate": 0.55, "recall_mrr": 0.45, "recall_precision": 0.28},
        },
        "longmemeval": {
            "overall_accuracy": 0.20,
            "category_accuracy": {"reasoning": 0.27, "extraction": 0.17},
            "metrics": {"recall_hit_rate": 0.18, "recall_mrr": 0.07, "recall_precision": 0.03},
        },
    }


# ---------------------------------------------------------------------------
# compare_benchmark — per-bench comparison
# ---------------------------------------------------------------------------


class TestCompareBenchmark:
    def test_identical_numbers_no_regressions(self) -> None:
        base = _baseline()["locomo"]
        actual = _results_at_baseline()["locomo"]
        regressions, rows = _CHECKER.compare_benchmark(
            "locomo", base, actual,
            overall_tolerance=0.02, category_tolerance=0.03, metric_tolerance=0.03,
        )
        assert regressions == []
        # 1 overall + 2 categories + 3 metrics = 6 rows
        assert len(rows) == 6

    def test_overall_drop_beyond_tolerance_flagged(self) -> None:
        base = _baseline()["locomo"]
        actual = dict(base)
        actual["overall_accuracy"] = 0.55  # -5pp, exceeds 2pp tolerance
        regressions, _ = _CHECKER.compare_benchmark(
            "locomo", base, actual,
            overall_tolerance=0.02, category_tolerance=0.03, metric_tolerance=0.03,
        )
        assert any("locomo:overall" in r for r in regressions)

    def test_category_drop_within_tolerance_not_flagged(self) -> None:
        """A 2pp drop on multi-hop with 3pp tolerance is a 'minor dip'
        status but not a regression — CI must not fail for noise."""
        base = _baseline()["locomo"]
        actual = dict(base)
        actual["category_accuracy"] = {"multi-hop": 0.13, "temporal": 0.70}
        regressions, _ = _CHECKER.compare_benchmark(
            "locomo", base, actual,
            overall_tolerance=0.02, category_tolerance=0.03, metric_tolerance=0.03,
        )
        assert regressions == []

    def test_metric_drop_beyond_tolerance_flagged(self) -> None:
        base = _baseline()["locomo"]
        actual = dict(base)
        actual["metrics"] = {"recall_hit_rate": 0.40, "recall_mrr": 0.45, "recall_precision": 0.28}
        regressions, _ = _CHECKER.compare_benchmark(
            "locomo", base, actual,
            overall_tolerance=0.02, category_tolerance=0.03, metric_tolerance=0.03,
        )
        assert any("recall_hit_rate" in r for r in regressions)

    def test_improvement_is_not_a_regression(self) -> None:
        """Large positive deltas are ✅ improvement, not ❌ regression.
        Otherwise the gate would fight against actually-landing wins."""
        base = _baseline()["locomo"]
        actual = dict(base)
        actual["overall_accuracy"] = 0.75  # +15pp
        regressions, rows = _CHECKER.compare_benchmark(
            "locomo", base, actual,
            overall_tolerance=0.02, category_tolerance=0.03, metric_tolerance=0.03,
        )
        assert regressions == []
        overall_row = next(r for r in rows if r[0] == "locomo:overall")
        assert overall_row[4] == "✅ improvement"

    def test_missing_category_in_actual_flagged(self) -> None:
        base = _baseline()["locomo"]
        actual = dict(base)
        actual["category_accuracy"] = {"temporal": 0.70}  # multi-hop missing
        regressions, _ = _CHECKER.compare_benchmark(
            "locomo", base, actual,
            overall_tolerance=0.02, category_tolerance=0.03, metric_tolerance=0.03,
        )
        assert any("multi-hop" in r for r in regressions)

    def test_new_category_in_actual_not_flagged(self) -> None:
        """If the actual result has a new category not in the baseline,
        that's fine — a new benchmark dimension isn't a regression."""
        base = _baseline()["locomo"]
        actual = dict(base)
        actual["category_accuracy"] = {
            "multi-hop": 0.15, "temporal": 0.70, "NEW_CATEGORY": 0.50,
        }
        regressions, _ = _CHECKER.compare_benchmark(
            "locomo", base, actual,
            overall_tolerance=0.02, category_tolerance=0.03, metric_tolerance=0.03,
        )
        assert regressions == []


# ---------------------------------------------------------------------------
# main() — end-to-end via the argparse surface
# ---------------------------------------------------------------------------


class TestMainEndToEnd:
    def _write(self, tmp_path: Path, baseline: dict, actual: dict) -> tuple[Path, Path]:
        bpath = tmp_path / "baseline.json"
        rpath = tmp_path / "results.json"
        bpath.write_text(json.dumps(baseline))
        rpath.write_text(json.dumps(actual))
        return bpath, rpath

    def test_clean_run_exits_zero(self, tmp_path: Path, capsys, monkeypatch) -> None:
        bpath, rpath = self._write(tmp_path, _baseline(), _results_at_baseline())
        monkeypatch.setattr(
            sys, "argv",
            ["check", "--baseline", str(bpath), "--results", str(rpath)],
        )
        rc = _CHECKER.main()
        assert rc == 0
        out = capsys.readouterr().out
        assert "No regressions detected" in out

    def test_regression_exits_one(self, tmp_path: Path, capsys, monkeypatch) -> None:
        broken = _results_at_baseline()
        broken["locomo"]["overall_accuracy"] = 0.30  # -30pp blowout
        bpath, rpath = self._write(tmp_path, _baseline(), broken)
        monkeypatch.setattr(
            sys, "argv",
            ["check", "--baseline", str(bpath), "--results", str(rpath)],
        )
        rc = _CHECKER.main()
        assert rc == 1
        out = capsys.readouterr().out
        assert "regression(s)" in out

    def test_missing_baseline_file_exits_two(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setattr(
            sys, "argv",
            ["check",
             "--baseline", str(tmp_path / "does-not-exist.json"),
             "--results", str(tmp_path / "also-missing.json")],
        )
        with pytest.raises(SystemExit) as excinfo:
            _CHECKER.main()
        assert excinfo.value.code == 2

    def test_malformed_baseline_exits_two(self, tmp_path: Path, monkeypatch) -> None:
        bpath = tmp_path / "bad.json"
        rpath = tmp_path / "ok.json"
        bpath.write_text("{not valid json")
        rpath.write_text(json.dumps(_results_at_baseline()))
        monkeypatch.setattr(
            sys, "argv",
            ["check", "--baseline", str(bpath), "--results", str(rpath)],
        )
        with pytest.raises(SystemExit) as excinfo:
            _CHECKER.main()
        assert excinfo.value.code == 2

    def test_tolerance_flags_flow_through(self, tmp_path: Path, capsys, monkeypatch) -> None:
        """A 2.5pp drop on overall with default 2pp tolerance flags;
        bumping tolerance to 5pp makes the same drop pass."""
        slight = _results_at_baseline()
        slight["locomo"]["overall_accuracy"] = 0.575  # -2.5pp
        bpath, rpath = self._write(tmp_path, _baseline(), slight)

        # Default tolerance — should flag.
        monkeypatch.setattr(sys, "argv",
            ["check", "--baseline", str(bpath), "--results", str(rpath)])
        assert _CHECKER.main() == 1

        # Loosened tolerance — same data, passes.
        monkeypatch.setattr(sys, "argv",
            ["check", "--baseline", str(bpath), "--results", str(rpath),
             "--overall-tolerance", "0.05"])
        assert _CHECKER.main() == 0


# ---------------------------------------------------------------------------
# GITHUB_STEP_SUMMARY emission
# ---------------------------------------------------------------------------


class TestGithubSummary:
    def test_summary_written_when_env_var_set(
        self, tmp_path: Path, monkeypatch,
    ) -> None:
        summary = tmp_path / "summary.md"
        monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))

        bpath = tmp_path / "baseline.json"
        rpath = tmp_path / "results.json"
        bpath.write_text(json.dumps(_baseline()))
        rpath.write_text(json.dumps(_results_at_baseline()))

        monkeypatch.setattr(sys, "argv",
            ["check", "--baseline", str(bpath), "--results", str(rpath)])
        _CHECKER.main()

        content = summary.read_text()
        assert "## Benchmark regression check" in content
        assert "| Metric | Baseline | Actual | Δ | Status |" in content
        assert "No regressions detected" in content

    def test_summary_flags_regressions(self, tmp_path: Path, monkeypatch) -> None:
        summary = tmp_path / "summary.md"
        monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))

        broken = _results_at_baseline()
        broken["locomo"]["overall_accuracy"] = 0.10

        bpath = tmp_path / "baseline.json"
        rpath = tmp_path / "results.json"
        bpath.write_text(json.dumps(_baseline()))
        rpath.write_text(json.dumps(broken))

        monkeypatch.setattr(sys, "argv",
            ["check", "--baseline", str(bpath), "--results", str(rpath)])
        _CHECKER.main()

        content = summary.read_text()
        assert "regression(s) detected" in content
        assert "❌ regression" in content

    def test_no_summary_when_env_var_unset(self, tmp_path: Path, monkeypatch) -> None:
        """Running locally (no CI) must not emit a summary file."""
        monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
        bpath = tmp_path / "baseline.json"
        rpath = tmp_path / "results.json"
        bpath.write_text(json.dumps(_baseline()))
        rpath.write_text(json.dumps(_results_at_baseline()))

        monkeypatch.setattr(sys, "argv",
            ["check", "--baseline", str(bpath), "--results", str(rpath)])
        # Should not crash even though GITHUB_STEP_SUMMARY is unset.
        _CHECKER.main()


# ---------------------------------------------------------------------------
# Checked-in baseline shape
# ---------------------------------------------------------------------------


def test_checked_in_baseline_is_well_formed() -> None:
    """The baseline JSON committed to the repo must be parseable and
    match the shape the script expects — otherwise the first CI run
    would hit an UnboundLocalError or equivalent."""
    baseline_path = _REPO_ROOT / "benchmarks" / "baselines-test-provider.json"
    assert baseline_path.exists(), "baseline JSON must exist at known path"
    data = json.loads(baseline_path.read_text())
    for bench_name, bench in data.items():
        assert "overall_accuracy" in bench, f"{bench_name} missing overall_accuracy"
        assert "category_accuracy" in bench, f"{bench_name} missing category_accuracy"
        assert "metrics" in bench, f"{bench_name} missing metrics"
