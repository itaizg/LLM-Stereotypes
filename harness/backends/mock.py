"""Deterministic fake backend used by every test and by CI.

No network, no model weights. Given the gold answer for a question (via
`context["question_id"]` and a `gold_answers` map) and a target accuracy per
condition, it returns the correct letter often enough, in a way that is
fully reproducible from `seed` — so tests can assert that harness.stats
recovers a known ground truth within its computed confidence interval.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping

from harness.backends.base import BackendDescriptor, ModelBackend

_LETTERS = ("A", "B", "C", "D")


def _unit_interval_hash(*parts: str) -> float:
    digest = hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()
    return int(digest[:16], 16) / 0xFFFFFFFFFFFFFFFF


class MockBackend(ModelBackend):
    def __init__(
        self,
        gold_answers: Mapping[str, str],
        target_accuracy: float | Mapping[str, float] = 0.6,
        *,
        extraction_failure_rate: float = 0.0,
    ) -> None:
        self._gold_answers = dict(gold_answers)
        self._target_accuracy = target_accuracy
        self._extraction_failure_rate = extraction_failure_rate

    @property
    def descriptor(self) -> BackendDescriptor:
        return BackendDescriptor(name="mock", model_id="mock-deterministic")

    def _target_for(self, condition_id: str | None) -> float:
        if isinstance(self._target_accuracy, Mapping):
            return self._target_accuracy.get(condition_id or "", 0.6)
        return self._target_accuracy

    def generate(
        self,
        prompt: str,
        *,
        seed: int,
        temperature: float,
        context: Mapping[str, str] | None = None,
    ) -> str:
        context = context or {}
        question_id = context.get("question_id", "")
        condition_id = context.get("condition_id")
        gold = self._gold_answers.get(question_id)

        roll = _unit_interval_hash(str(seed), question_id, condition_id or "")

        if self._extraction_failure_rate and roll < self._extraction_failure_rate:
            return "I'm not sure, this question is ambiguous."

        if gold is None:
            # No ground truth available: answer deterministically but arbitrarily.
            idx = (
                int(hashlib.sha256(f"{seed}|{question_id}".encode()).hexdigest(), 16)
                % 4
            )
            return _LETTERS[idx]

        correctness_roll = _unit_interval_hash(
            "correct", str(seed), question_id, condition_id or ""
        )
        if correctness_roll < self._target_for(condition_id):
            return gold

        wrong_letters = [letter for letter in _LETTERS if letter != gold]
        idx = int(
            hashlib.sha256(f"wrong|{seed}|{question_id}".encode()).hexdigest(), 16
        ) % len(wrong_letters)
        return wrong_letters[idx]
