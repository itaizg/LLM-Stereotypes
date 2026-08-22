import numpy as np

from harness.stats import (
    ScoreRow,
    bootstrap_accuracy,
    holm_correction,
    paired_contrast,
    required_n_questions,
)


def _make_rows(condition_id, question_answers):
    """question_answers: dict[question_id -> list[bool]] (one per sample)."""
    rows = []
    for qid, samples in question_answers.items():
        for i, is_correct in enumerate(samples):
            rows.append(
                ScoreRow(
                    condition_id=condition_id,
                    question_id=qid,
                    sample_idx=i,
                    extracted_answer="A" if is_correct else "B",
                    gold_answer="A",
                    is_correct=bool(is_correct),
                    is_extraction_failure=False,
                )
            )
    return rows


def test_bootstrap_accuracy_recovers_known_ground_truth():
    rng = np.random.default_rng(0)
    n_questions, n_samples, target = 30, 20, 0.7
    question_answers = {
        f"q{i}": list(rng.random(n_samples) < target) for i in range(n_questions)
    }
    rows = _make_rows("cond_a", question_answers)

    result = bootstrap_accuracy(rows, "cond_a", seed=1)

    assert abs(result.accuracy - target) < 0.1
    assert result.ci_low <= result.accuracy <= result.ci_high
    assert result.n_questions == n_questions


def test_bootstrap_accuracy_reports_extraction_failure_rate():
    rows = [
        ScoreRow(
            "cond", "q1", 0, "", "A", is_correct=False, is_extraction_failure=True
        ),
        ScoreRow(
            "cond", "q1", 1, "A", "A", is_correct=True, is_extraction_failure=False
        ),
        ScoreRow(
            "cond", "q2", 0, "B", "A", is_correct=False, is_extraction_failure=False
        ),
        ScoreRow(
            "cond", "q2", 1, "B", "A", is_correct=False, is_extraction_failure=False
        ),
    ]
    result = bootstrap_accuracy(rows, "cond", seed=0)
    assert (
        result.extraction_failure_rate == 0.25
    )  # mean of per-question rates: 0.5 and 0.0


def test_paired_contrast_detects_injected_delta():
    rng = np.random.default_rng(0)
    n_questions, n_samples = 30, 20
    control_p, treatment_p = 0.5, 0.8

    control_answers = {
        f"q{i}": list(rng.random(n_samples) < control_p) for i in range(n_questions)
    }
    treatment_answers = {
        f"q{i}": list(rng.random(n_samples) < treatment_p) for i in range(n_questions)
    }
    rows = _make_rows("control", control_answers) + _make_rows(
        "treatment", treatment_answers
    )

    contrast = paired_contrast(rows, "treatment", "control", seed=1)

    assert contrast.distinguishable_from_noise
    assert contrast.mean_diff > 0
    assert "increased" in contrast.describe()


def test_paired_contrast_reports_noise_when_no_true_effect():
    rng = np.random.default_rng(0)
    n_questions, n_samples, p = 30, 20, 0.6

    control_answers = {
        f"q{i}": list(rng.random(n_samples) < p) for i in range(n_questions)
    }
    treatment_answers = {
        f"q{i}": list(rng.random(n_samples) < p) for i in range(n_questions)
    }
    rows = _make_rows("control", control_answers) + _make_rows(
        "treatment", treatment_answers
    )

    contrast = paired_contrast(rows, "treatment", "control", seed=1)

    assert contrast.describe() == "not distinguishable from noise"
    assert not contrast.distinguishable_from_noise


def test_holm_correction_never_makes_pvalues_smaller():
    pvalues = [0.001, 0.04, 0.03, 0.5]
    adjusted = holm_correction(pvalues)
    assert len(adjusted) == len(pvalues)
    for raw, adj in zip(pvalues, adjusted):
        assert adj >= raw


def test_holm_correction_is_monotonic_in_sorted_order():
    pvalues = [0.001, 0.04, 0.03, 0.5]
    adjusted = holm_correction(pvalues)
    order = sorted(range(len(pvalues)), key=lambda i: pvalues[i])
    adjusted_in_order = [adjusted[i] for i in order]
    assert adjusted_in_order == sorted(adjusted_in_order)


def test_holm_correction_all_ones_when_pvalues_are_one():
    assert holm_correction([1.0, 1.0, 1.0]) == [1.0, 1.0, 1.0]


def test_required_n_questions_is_monotonic_in_delta():
    n_large_delta = required_n_questions(0.3, discordant_rate=0.5)
    n_small_delta = required_n_questions(0.05, discordant_rate=0.5)
    assert n_small_delta > n_large_delta


def test_required_n_questions_matches_known_formula():
    # alpha=.05, power=.8 -> (z_alpha/2 + z_beta)^2 ~= 7.849
    n = required_n_questions(0.2, discordant_rate=1.0, alpha=0.05, power=0.8)
    assert 190 <= n <= 200
