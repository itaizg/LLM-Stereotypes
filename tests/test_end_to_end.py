from pathlib import Path

from harness.backends.mock import MockBackend
from harness.config import load_suite
from harness.report import compare_runs, compute_stats, write_report
from harness.runner import run_suite


def _run(
    suite_dir: Path,
    tmp_path: Path,
    target_accuracy,
    model_label="e2e-model",
    n_samples=15,
    cache_name="cache.sqlite3",
    results_subdir="results",
):
    config = load_suite(suite_dir)
    gold_answers = {q.question_id: q.answer for q in config.questions}
    backend = MockBackend(gold_answers, target_accuracy=target_accuracy)
    return run_suite(
        config,
        backend,
        model_label=model_label,
        n_samples=n_samples,
        results_dir=tmp_path / results_subdir,
        cache_path=tmp_path / results_subdir / "cache" / cache_name,
    )


def test_end_to_end_run_recovers_injected_accuracy(
    suites_root: Path, tmp_path: Path
) -> None:
    run_dir = _run(suites_root / "stereotype_threat", tmp_path, target_accuracy=0.6)

    stats = compute_stats(run_dir, suites_root=suites_root)

    assert len(stats["accuracies"]) == 12
    for accuracy in stats["accuracies"].values():
        assert (
            accuracy["ci_low"] <= 0.6 <= accuracy["ci_high"]
            or abs(accuracy["accuracy"] - 0.6) < 0.2
        )

    assert len(stats["contrasts"]) == 9  # 9 treatment conditions
    # Every condition was generated at the same target accuracy, so with
    # Holm correction across the 9 simultaneous contrasts, none should
    # register as significant. (The uncorrected per-contrast CI can, by
    # chance, flag ~1 in 20 at alpha=.05 -- that's exactly what Holm
    # correction exists to guard against, so it's the adjusted p-value
    # that should be checked here, not the raw description.)
    for contrast in stats["contrasts"].values():
        assert contrast["mcnemar_p_holm"] >= 0.05


def test_end_to_end_write_report_produces_all_artifacts(
    suites_root: Path, tmp_path: Path
) -> None:
    run_dir = _run(
        suites_root / "stereotype_threat", tmp_path, target_accuracy=0.6, n_samples=5
    )

    report_path = write_report(run_dir, suites_root=suites_root)

    assert report_path.exists()
    assert (run_dir / "stats.json").exists()
    report_text = report_path.read_text()
    assert "Per-condition accuracy" in report_text
    assert "Condition contrasts" in report_text


def test_compare_runs_detects_injected_difference_between_two_runs(
    suites_root: Path, tmp_path: Path
) -> None:
    run_a = _run(
        suites_root / "stereotype_threat",
        tmp_path,
        target_accuracy=0.5,
        model_label="model-a",
        cache_name="a.sqlite3",
    )
    run_b = _run(
        suites_root / "stereotype_threat",
        tmp_path,
        target_accuracy=0.85,
        model_label="model-b",
        cache_name="b.sqlite3",
    )

    comparison = compare_runs(run_a, run_b, suites_root=suites_root)

    assert len(comparison["comparisons"]) == 12
    for result in comparison["comparisons"].values():
        assert result["description"] != "not distinguishable from noise"
        assert result["mean_diff"] > 0


def test_compare_runs_reports_noise_for_two_equivalent_runs(
    suites_root: Path, tmp_path: Path
) -> None:
    # Same model label (and so the same deterministic MockBackend seeds)
    # in two separate run artifacts -- e.g. "did rerunning the exact same
    # config change anything" -- should show exactly zero difference,
    # deterministically, not just a difference too small to detect.
    run_a = _run(
        suites_root / "stereotype_threat",
        tmp_path,
        target_accuracy=0.6,
        model_label="same-model",
        results_subdir="results_a",
    )
    run_b = _run(
        suites_root / "stereotype_threat",
        tmp_path,
        target_accuracy=0.6,
        model_label="same-model",
        results_subdir="results_b",
    )

    comparison = compare_runs(run_a, run_b, suites_root=suites_root)

    for result in comparison["comparisons"].values():
        assert result["mean_diff"] == 0.0
        assert result["description"] == "not distinguishable from noise"
