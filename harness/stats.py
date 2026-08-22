"""Condition-contrast statistics — the actual product of this harness.

Every reported number carries a confidence interval, computed via a
cluster bootstrap over *questions*, not individual samples: with
n_samples repeated generations per question, observations are clustered
by question, and a naive bootstrap over every (question, sample) pair
would understate the interval by treating correlated observations as
independent. That is the single most important statistical choice here.

The reporting rule this module exists to enforce: a contrast whose CI
straddles zero is "not distinguishable from noise", full stop — never
narrated as an improvement or a deterioration. ContrastResult.describe()
is the one place that renders a contrast to text, so nothing downstream
can accidentally bypass the rule.
"""

from __future__ import annotations

import csv
import math
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from scipy import stats as scipy_stats


@dataclass(frozen=True)
class ScoreRow:
    condition_id: str
    question_id: str
    sample_idx: int
    extracted_answer: str
    gold_answer: str
    is_correct: bool
    is_extraction_failure: bool


def load_scores(csv_path: Path | str) -> list[ScoreRow]:
    rows = []
    with open(csv_path, newline="") as f:
        for r in csv.DictReader(f):
            rows.append(
                ScoreRow(
                    condition_id=r["condition_id"],
                    question_id=r["question_id"],
                    sample_idx=int(r["sample_idx"]),
                    extracted_answer=r["extracted_answer"],
                    gold_answer=r["gold_answer"],
                    is_correct=bool(int(r["is_correct"])),
                    is_extraction_failure=bool(int(r["is_extraction_failure"])),
                )
            )
    return rows


def per_question_means(
    rows: list[ScoreRow], condition_id: str, field: str = "is_correct"
) -> dict[str, float]:
    buckets: dict[str, list[bool]] = defaultdict(list)
    for row in rows:
        if row.condition_id == condition_id:
            buckets[row.question_id].append(getattr(row, field))
    return {qid: float(np.mean(values)) for qid, values in buckets.items()}


def _bootstrap_means(values: np.ndarray, n_bootstrap: int, seed: int) -> np.ndarray:
    n = len(values)
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, n, size=(n_bootstrap, n))
    return values[idx].mean(axis=1)


@dataclass(frozen=True)
class AccuracyResult:
    condition_id: str
    n_questions: int
    accuracy: float
    ci_low: float
    ci_high: float
    extraction_failure_rate: float


def bootstrap_accuracy(
    rows: list[ScoreRow],
    condition_id: str,
    *,
    n_bootstrap: int = 2000,
    seed: int = 0,
) -> AccuracyResult:
    per_question = per_question_means(rows, condition_id)
    if not per_question:
        raise ValueError(f"no rows for condition {condition_id!r}")

    question_ids = sorted(per_question)
    values = np.array([per_question[q] for q in question_ids])

    boot_means = _bootstrap_means(values, n_bootstrap, seed)
    ci_low, ci_high = np.percentile(boot_means, [2.5, 97.5])

    failure_rates = per_question_means(
        rows, condition_id, field="is_extraction_failure"
    )
    overall_failure_rate = (
        float(np.mean(list(failure_rates.values()))) if failure_rates else 0.0
    )

    return AccuracyResult(
        condition_id=condition_id,
        n_questions=len(values),
        accuracy=float(values.mean()),
        ci_low=float(ci_low),
        ci_high=float(ci_high),
        extraction_failure_rate=overall_failure_rate,
    )


@dataclass(frozen=True)
class ContrastResult:
    treatment_id: str
    control_id: str
    n_questions: int
    mean_diff: float
    ci_low: float
    ci_high: float
    mcnemar_b: int  # control correct, treatment incorrect
    mcnemar_c: int  # control incorrect, treatment correct
    mcnemar_p: float

    @property
    def distinguishable_from_noise(self) -> bool:
        return not (self.ci_low <= 0 <= self.ci_high)

    def describe(self) -> str:
        """The one place a contrast is rendered to text. A delta whose CI
        straddles zero is always reported as noise — never as an
        improvement or a deterioration."""
        if not self.distinguishable_from_noise:
            return "not distinguishable from noise"
        direction = "increased" if self.mean_diff > 0 else "decreased"
        return f"{direction} accuracy by {abs(self.mean_diff) * 100:.1f} points"


def paired_contrast(
    rows: list[ScoreRow],
    treatment_id: str,
    control_id: str,
    *,
    n_bootstrap: int = 2000,
    seed: int = 0,
) -> ContrastResult:
    """Contrast two conditions within one run's rows."""
    treatment = per_question_means(rows, treatment_id)
    control = per_question_means(rows, control_id)
    return contrast_from_means(
        treatment, control, treatment_id, control_id, n_bootstrap=n_bootstrap, seed=seed
    )


def contrast_from_means(
    treatment: dict[str, float],
    control: dict[str, float],
    treatment_id: str,
    control_id: str,
    *,
    n_bootstrap: int = 2000,
    seed: int = 0,
) -> ContrastResult:
    """Contrast two already-computed per-question-mean maps.

    This is what `paired_contrast` uses internally, and also what
    `compare_runs` (harness/report.py) uses to contrast the same
    condition across two *separate* run artifacts — the prompt-regression
    / model-upgrade gate — without needing to fake a shared rows list.
    """
    shared_questions = sorted(set(treatment) & set(control))
    if not shared_questions:
        raise ValueError(
            f"no shared questions between {treatment_id!r} and {control_id!r}"
        )

    t_values = np.array([treatment[q] for q in shared_questions])
    c_values = np.array([control[q] for q in shared_questions])
    diffs = t_values - c_values

    boot_means = _bootstrap_means(diffs, n_bootstrap, seed)
    ci_low, ci_high = np.percentile(boot_means, [2.5, 97.5])

    # McNemar on majority-vote binarization per question (a secondary,
    # classically-paired test alongside the primary bootstrap CI above).
    t_binary = t_values >= 0.5
    c_binary = c_values >= 0.5
    b = int(np.sum(c_binary & ~t_binary))  # control correct, treatment incorrect
    c = int(np.sum(~c_binary & t_binary))  # control incorrect, treatment correct
    if b + c == 0:
        mcnemar_p = 1.0
    else:
        mcnemar_p = scipy_stats.binomtest(
            min(b, c), b + c, 0.5, alternative="two-sided"
        ).pvalue

    return ContrastResult(
        treatment_id=treatment_id,
        control_id=control_id,
        n_questions=len(diffs),
        mean_diff=float(diffs.mean()),
        ci_low=float(ci_low),
        ci_high=float(ci_high),
        mcnemar_b=b,
        mcnemar_c=c,
        mcnemar_p=float(mcnemar_p),
    )


def holm_correction(pvalues: list[float]) -> list[float]:
    """Holm-Bonferroni step-down adjustment. Returns adjusted p-values in
    the same order as the input — with ~12 conditions, uncorrected
    pairwise p-values are indefensible."""
    m = len(pvalues)
    order = sorted(range(m), key=lambda i: pvalues[i])
    adjusted = [0.0] * m
    running_max = 0.0
    for rank, i in enumerate(order):
        adj = (m - rank) * pvalues[i]
        running_max = max(running_max, adj)
        adjusted[i] = min(running_max, 1.0)
    return adjusted


def required_n_questions(
    delta: float,
    *,
    discordant_rate: float = 0.5,
    alpha: float = 0.05,
    power: float = 0.8,
) -> int:
    """Required number of paired questions to detect a net McNemar effect
    of `delta` = p(treatment right, control wrong) - p(control right,
    treatment wrong), given an assumed discordant-pair rate (p01 + p10,
    the fraction of questions where the two conditions disagree at all —
    pass the observed rate from a pilot run when available).

    Formalizes the calculation the original README sketched with a
    two-proportion Z-score (README "Statistical significance" section)
    using the paired-design analogue instead.
    """
    if delta <= 0:
        raise ValueError("delta must be positive")
    if not (0 < discordant_rate <= 1):
        raise ValueError("discordant_rate must be in (0, 1]")
    z_alpha = scipy_stats.norm.ppf(1 - alpha / 2)
    z_beta = scipy_stats.norm.ppf(power)
    n = ((z_alpha + z_beta) ** 2 * discordant_rate) / (delta**2)
    return math.ceil(n)


def power_analysis_for_contrast(
    contrast: ContrastResult,
    *,
    alpha: float = 0.05,
    power: float = 0.8,
) -> int | None:
    """Required N to detect the observed effect size at the given alpha
    and power, using the observed discordant rate as the assumed rate.
    None if there's no observed effect (or no disagreement at all) to
    size against.
    """
    if contrast.mean_diff == 0:
        return None
    discordant_rate = (contrast.mcnemar_b + contrast.mcnemar_c) / contrast.n_questions
    if discordant_rate == 0:
        return None
    return required_n_questions(
        abs(contrast.mean_diff),
        discordant_rate=discordant_rate,
        alpha=alpha,
        power=power,
    )
