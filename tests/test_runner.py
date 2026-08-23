from pathlib import Path

from harness.backends.mock import MockBackend
from harness.config import load_suite
from harness.runner import run_suite

SUITE_DIR = Path(__file__).resolve().parent.parent / "suites" / "stereotype_threat"


class CountingBackend:
    """Wraps a backend and counts real generate() calls, to prove resume
    skips them via the cache rather than by accident."""

    def __init__(self, inner):
        self._inner = inner
        self.call_count = 0

    @property
    def descriptor(self):
        return self._inner.descriptor

    def generate(self, prompt, *, seed, temperature, context=None):
        self.call_count += 1
        return self._inner.generate(
            prompt, seed=seed, temperature=temperature, context=context
        )


def _gold_answers(config):
    return {q.question_id: q.answer for q in config.questions}


def test_run_suite_writes_all_artifacts(tmp_path):
    config = load_suite(SUITE_DIR)
    backend = MockBackend(_gold_answers(config), target_accuracy=0.7)

    run_dir = run_suite(
        config,
        backend,
        model_label="test-model",
        n_samples=2,
        results_dir=tmp_path / "results",
    )

    assert (run_dir / "manifest.json").exists()
    assert (run_dir / "responses.jsonl").exists()
    assert (run_dir / "scores.csv").exists()

    with open(run_dir / "scores.csv") as f:
        lines = f.readlines()
    assert len(lines) == 1 + 12 * 31 * 2  # header + conditions * questions * samples


def test_run_suite_resume_makes_zero_backend_calls_on_second_run(tmp_path):
    config = load_suite(SUITE_DIR)
    cache_path = tmp_path / "shared_cache.sqlite3"

    backend_1 = CountingBackend(MockBackend(_gold_answers(config), target_accuracy=0.6))
    run_suite(
        config,
        backend_1,
        model_label="test-model",
        n_samples=1,
        results_dir=tmp_path / "results",
        cache_path=cache_path,
    )
    assert backend_1.call_count == 12 * 31 * 1

    backend_2 = CountingBackend(MockBackend(_gold_answers(config), target_accuracy=0.6))
    run_suite(
        config,
        backend_2,
        model_label="test-model",
        n_samples=1,
        results_dir=tmp_path / "results",
        cache_path=cache_path,
    )
    assert backend_2.call_count == 0


def test_run_suite_partial_overlap_only_calls_backend_for_new_jobs(tmp_path):
    config = load_suite(SUITE_DIR)
    cache_path = tmp_path / "shared_cache.sqlite3"

    backend_1 = CountingBackend(MockBackend(_gold_answers(config), target_accuracy=0.6))
    run_suite(
        config,
        backend_1,
        model_label="test-model",
        n_samples=1,
        results_dir=tmp_path / "results",
        cache_path=cache_path,
    )

    # Requesting 2 samples reuses sample_idx=0 from the cache and only
    # generates the new sample_idx=1 for every (condition, question).
    backend_2 = CountingBackend(MockBackend(_gold_answers(config), target_accuracy=0.6))
    run_suite(
        config,
        backend_2,
        model_label="test-model",
        n_samples=2,
        results_dir=tmp_path / "results",
        cache_path=cache_path,
    )
    assert backend_2.call_count == 12 * 31 * 1
