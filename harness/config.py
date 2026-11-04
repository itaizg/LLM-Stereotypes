"""Suite config loading and job expansion.

A suite is a directory (see suites/stereotype_threat/) holding
conditions.yaml (identities × stereotypes × conditions) and a questions
JSON file. Loading it never touches a model — adding a new suite is
strictly a config-authoring exercise, never a harness code change.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

import yaml


@dataclass(frozen=True)
class Identity:
    id: str
    text: str


@dataclass(frozen=True)
class Stereotype:
    id: str
    text: str | None


@dataclass(frozen=True)
class Condition:
    id: str
    identity: str
    stereotype: str
    role: str
    control_of: str | None = None


@dataclass(frozen=True)
class Question:
    question_id: str
    index: int
    question: str
    choices: tuple[str, ...]
    answer: str


@dataclass(frozen=True)
class SuiteConfig:
    suite: str
    identities: dict[str, Identity]
    stereotypes: dict[str, Stereotype]
    conditions: tuple[Condition, ...]
    questions: tuple[Question, ...]
    n_samples_default: int
    temperature_default: float
    config_hash: str

    def condition(self, condition_id: str) -> Condition:
        for c in self.conditions:
            if c.id == condition_id:
                return c
        raise KeyError(f"unknown condition: {condition_id}")

    def assemble_prompt(self, condition_id: str, question: Question) -> str:
        """Identity + stereotype (if any) + question stem + choices.

        Fixes legacy/BUGS.md bug #3: a newline always separates the stem
        from the choice block, so the first choice never gets glued onto
        the stem's last line.
        """
        condition = self.condition(condition_id)
        identity = self.identities[condition.identity]
        stereotype = self.stereotypes[condition.stereotype]

        parts = [identity.text]
        if stereotype.text:
            parts.append(stereotype.text)
        condition_prompt = "\n".join(parts)

        choice_block = "\n".join(question.choices)
        return f"{condition_prompt}\n{question.question}\n{choice_block}"


_ROLES = ("control", "treatment")


def _validate_contrasts(conditions: tuple[Condition, ...]) -> None:
    """Reject malformed roles and contrasts at load time.

    Without this, a missing or misspelled `control_of`, a misspelled role
    (silently dropped from the stats) or a control from another identity
    group (a contrast that measures the wrong thing) only surfaces after
    every generation for the run has been paid for.
    """
    by_id = {c.id: c for c in conditions}
    for c in conditions:
        if c.role not in _ROLES:
            raise ValueError(
                f"condition {c.id!r} has role {c.role!r}, expected one of {_ROLES}"
            )
    for c in conditions:
        if c.role != "treatment":
            continue
        control = by_id.get(c.control_of) if c.control_of else None
        if control is None or control.role != "control":
            raise ValueError(
                f"treatment condition {c.id!r} must set control_of to a "
                f"declared control condition, got {c.control_of!r}"
            )
        if control.identity != c.identity:
            raise ValueError(
                f"treatment condition {c.id!r} (identity {c.identity!r}) "
                f"has control {control.id!r} from a different identity "
                f"({control.identity!r})"
            )


def load_suite(suite_dir: Path | str) -> SuiteConfig:
    suite_dir = Path(suite_dir)
    conditions_path = suite_dir / "conditions.yaml"
    with open(conditions_path) as f:
        raw = yaml.safe_load(f)

    questions_path = suite_dir / raw["questions_file"]
    with open(questions_path) as f:
        raw_questions = json.load(f)

    identities = {
        iid: Identity(id=iid, text=data["text"])
        for iid, data in raw["identities"].items()
    }
    stereotypes = {
        sid: Stereotype(id=sid, text=data.get("text"))
        for sid, data in raw["stereotypes"].items()
    }
    conditions = tuple(
        Condition(
            id=c["id"],
            identity=c["identity"],
            stereotype=c["stereotype"],
            role=c["role"],
            control_of=c.get("control_of"),
        )
        for c in raw["conditions"]
    )
    _validate_contrasts(conditions)
    questions = tuple(
        Question(
            question_id=q["question_id"],
            index=q["index"],
            question=q["question"],
            choices=tuple(q["choices"]),
            answer=q["answer"],
        )
        for q in raw_questions["questions"]
    )

    config_hash = hashlib.sha256(
        (conditions_path.read_text() + questions_path.read_text()).encode("utf-8")
    ).hexdigest()[:8]

    return SuiteConfig(
        suite=raw["suite"],
        identities=identities,
        stereotypes=stereotypes,
        conditions=conditions,
        questions=questions,
        n_samples_default=raw.get("n_samples_default", 25),
        temperature_default=raw.get("temperature_default", 0.7),
        config_hash=config_hash,
    )


@dataclass(frozen=True)
class Job:
    model: str
    condition_id: str
    question_id: str
    sample_idx: int
    seed: int


def _job_seed(model: str, condition_id: str, question_id: str, sample_idx: int) -> int:
    digest = hashlib.sha256(
        f"{model}|{condition_id}|{question_id}|{sample_idx}".encode()
    ).hexdigest()
    return int(digest[:8], 16)


def expand_jobs(config: SuiteConfig, models: list[str], n_samples: int) -> list[Job]:
    """(models x conditions x questions x n_samples) -> jobs.

    Seeds are deterministic functions of (model, condition, question,
    sample_idx), not randomly drawn, so a run is exactly reproducible from
    its config and the same seeds resume cleanly through the cache.
    """
    jobs = []
    for model in models:
        for condition in config.conditions:
            for question in config.questions:
                for sample_idx in range(n_samples):
                    seed = _job_seed(
                        model, condition.id, question.question_id, sample_idx
                    )
                    jobs.append(
                        Job(model, condition.id, question.question_id, sample_idx, seed)
                    )
    return jobs
