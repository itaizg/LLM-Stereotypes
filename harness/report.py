"""Renders a run artifact directory to stats.json + report.md (+ plots),
and `compare_runs` between two run artifacts — the prompt-regression /
model-upgrade gate this harness is really for: point it at a baseline run
and a candidate run (different prompt, different model) to get a
per-condition delta with a CI, instead of vibes.
"""

from __future__ import annotations

import json
from pathlib import Path

from harness.config import load_suite
from harness.stats import (
    AccuracyResult,
    ContrastResult,
    bootstrap_accuracy,
    contrast_from_means,
    holm_correction,
    load_scores,
    paired_contrast,
    per_question_means,
    power_analysis_for_contrast,
)


def _suite_dir_for(suite: str, suites_root: Path | str) -> Path:
    return Path(suites_root) / suite


def _accuracy_to_dict(a: AccuracyResult) -> dict:
    return {
        "condition_id": a.condition_id,
        "n_questions": a.n_questions,
        "accuracy": a.accuracy,
        "ci_low": a.ci_low,
        "ci_high": a.ci_high,
        "extraction_failure_rate": a.extraction_failure_rate,
    }


def _contrast_to_dict(
    c: ContrastResult, adjusted_p: float, required_n: int | None
) -> dict:
    return {
        "treatment_id": c.treatment_id,
        "control_id": c.control_id,
        "n_questions": c.n_questions,
        "mean_diff": c.mean_diff,
        "ci_low": c.ci_low,
        "ci_high": c.ci_high,
        "mcnemar_p": c.mcnemar_p,
        "mcnemar_p_holm": adjusted_p,
        "distinguishable_from_noise": c.distinguishable_from_noise,
        "description": c.describe(),
        "required_n_questions_for_observed_effect": required_n,
    }


def compute_stats(run_dir: Path | str, suites_root: Path | str = "suites") -> dict:
    run_dir = Path(run_dir)
    with open(run_dir / "manifest.json") as f:
        manifest = json.load(f)

    rows = load_scores(run_dir / "scores.csv")
    config = load_suite(_suite_dir_for(manifest["suite"], suites_root))

    accuracies = {c.id: bootstrap_accuracy(rows, c.id) for c in config.conditions}

    treatment_conditions = [c for c in config.conditions if c.role == "treatment"]
    # load_suite guarantees every treatment has a control; the walrus only
    # narrows `str | None` for the type checker.
    contrasts = {
        c.id: paired_contrast(rows, c.id, control_id)
        for c in treatment_conditions
        if (control_id := c.control_of) is not None
    }

    pvalues = [contrasts[c.id].mcnemar_p for c in treatment_conditions]
    adjusted = holm_correction(pvalues) if pvalues else []
    adjusted_pvalues = {
        c.id: adj for c, adj in zip(treatment_conditions, adjusted, strict=True)
    }

    power = {
        c.id: power_analysis_for_contrast(contrasts[c.id]) for c in treatment_conditions
    }

    return {
        "run_id": run_dir.name,
        "manifest": manifest,
        "accuracies": {cid: _accuracy_to_dict(a) for cid, a in accuracies.items()},
        "contrasts": {
            cid: _contrast_to_dict(contrast, adjusted_pvalues[cid], power[cid])
            for cid, contrast in contrasts.items()
        },
    }


def write_stats_json(stats: dict, run_dir: Path | str) -> Path:
    path = Path(run_dir) / "stats.json"
    with open(path, "w") as f:
        json.dump(stats, f, indent=2)
        f.write("\n")
    return path


def render_report_markdown(stats: dict) -> str:
    manifest = stats["manifest"]
    backend = manifest["backend"]
    revision = f" @ `{backend['revision']}`" if backend.get("revision") else ""

    lines = [
        f"# {stats['run_id']}",
        "",
        f"- backend: `{backend['name']}` / model `{backend['model_id']}`{revision}",
        f"- device: `{backend.get('device') or 'n/a'}`",
        f"- n_samples: {manifest['n_samples']}, temperature: {manifest['temperature']}",
        f"- git: `{manifest['git_sha']}`"
        + (" (dirty)" if manifest["git_dirty"] else ""),
        "",
        "## Per-condition accuracy",
        "",
        "| Condition | N questions | Accuracy | 95% CI | Extraction failure rate |",
        "|---|---:|---:|---|---:|",
    ]
    for cid, a in stats["accuracies"].items():
        lines.append(
            f"| {cid} | {a['n_questions']} | {a['accuracy'] * 100:.1f}% "
            f"| [{a['ci_low'] * 100:.1f}%, {a['ci_high'] * 100:.1f}%] "
            f"| {a['extraction_failure_rate'] * 100:.1f}% |"
        )

    lines += [
        "",
        "## Condition contrasts (vs. designated control arm)",
        "",
        (
            "A delta whose CI includes zero is reported as noise — never as an "
            "improvement or a deterioration."
        ),
        "",
        (
            "| Treatment | Control | Δ | 95% CI | McNemar p (Holm) | Result "
            "| N needed for this effect |"
        ),
        "|---|---|---:|---|---:|---|---:|",
    ]
    for c in stats["contrasts"].values():
        required_n = c["required_n_questions_for_observed_effect"]
        lines.append(
            f"| {c['treatment_id']} | {c['control_id']} "
            f"| {c['mean_diff'] * 100:+.1f}pts "
            f"| [{c['ci_low'] * 100:+.1f}, {c['ci_high'] * 100:+.1f}] "
            f"| {c['mcnemar_p_holm']:.3f} | {c['description']} "
            f"| {required_n if required_n is not None else 'n/a'} |"
        )

    return "\n".join(lines) + "\n"


def _write_accuracy_plot(stats: dict, run_dir: Path) -> None:
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        return

    plots_dir = run_dir / "plots"
    plots_dir.mkdir(exist_ok=True)

    conditions = list(stats["accuracies"])
    accuracies = [stats["accuracies"][c]["accuracy"] * 100 for c in conditions]
    lower_err = [
        accuracies[i] - stats["accuracies"][c]["ci_low"] * 100
        for i, c in enumerate(conditions)
    ]
    upper_err = [
        stats["accuracies"][c]["ci_high"] * 100 - accuracies[i]
        for i, c in enumerate(conditions)
    ]

    fig, ax = plt.subplots(figsize=(max(6, len(conditions) * 0.8), 5))
    ax.bar(conditions, accuracies, yerr=[lower_err, upper_err], capsize=4)
    ax.set_ylabel("Accuracy (%)")
    ax.set_title(stats["run_id"])
    plt.setp(ax.get_xticklabels(), rotation=45, ha="right")
    fig.tight_layout()
    fig.savefig(plots_dir / "accuracy.png")
    plt.close(fig)


def write_report(run_dir: Path | str, suites_root: Path | str = "suites") -> Path:
    run_dir = Path(run_dir)
    stats = compute_stats(run_dir, suites_root)
    write_stats_json(stats, run_dir)
    report_path = run_dir / "report.md"
    with open(report_path, "w") as f:
        f.write(render_report_markdown(stats))
    _write_accuracy_plot(stats, run_dir)
    return report_path


def compare_runs(
    run_a_dir: Path | str, run_b_dir: Path | str, suites_root: Path | str = "suites"
) -> dict:
    run_a_dir, run_b_dir = Path(run_a_dir), Path(run_b_dir)
    with open(run_a_dir / "manifest.json") as f:
        manifest_a = json.load(f)
    with open(run_b_dir / "manifest.json") as f:
        manifest_b = json.load(f)

    rows_a = load_scores(run_a_dir / "scores.csv")
    rows_b = load_scores(run_b_dir / "scores.csv")

    condition_ids_a = {r.condition_id for r in rows_a}
    condition_ids_b = {r.condition_id for r in rows_b}
    shared_conditions = sorted(condition_ids_a & condition_ids_b)

    comparisons = {}
    for cid in shared_conditions:
        control_means = per_question_means(rows_a, cid)  # run A is the baseline
        treatment_means = per_question_means(rows_b, cid)  # run B is the candidate
        contrast = contrast_from_means(
            treatment_means,
            control_means,
            f"{run_b_dir.name}:{cid}",
            f"{run_a_dir.name}:{cid}",
        )
        comparisons[cid] = {
            "run_a_accuracy": sum(control_means.values()) / len(control_means),
            "run_b_accuracy": sum(treatment_means.values()) / len(treatment_means),
            "mean_diff": contrast.mean_diff,
            "ci_low": contrast.ci_low,
            "ci_high": contrast.ci_high,
            "description": contrast.describe(),
        }

    return {
        "run_a": run_a_dir.name,
        "run_a_suite": manifest_a["suite"],
        "run_b": run_b_dir.name,
        "run_b_suite": manifest_b["suite"],
        "comparisons": comparisons,
    }
