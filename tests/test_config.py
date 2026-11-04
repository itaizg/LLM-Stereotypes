import shutil
from pathlib import Path
from typing import Any

import pytest
import yaml

from harness.config import expand_jobs, load_suite


def test_load_suite_has_12_conditions_and_31_questions(suite_dir: Path) -> None:
    config = load_suite(suite_dir)
    assert len(config.conditions) == 12
    assert len(config.questions) == 31


def test_load_suite_control_conditions_have_no_control_of(suite_dir: Path) -> None:
    config = load_suite(suite_dir)
    controls = [c for c in config.conditions if c.role == "control"]
    treatments = [c for c in config.conditions if c.role == "treatment"]
    assert len(controls) == 3
    assert len(treatments) == 9
    for c in controls:
        assert c.control_of is None
    for t in treatments:
        assert t.control_of is not None
        # every control_of points at a real, declared control condition
        referenced = config.condition(t.control_of)
        assert referenced.role == "control"


def _copy_suite_with_edit(
    suite_dir: Path, tmp_path: Path
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Copy the test suite to tmp_path/suite; return raw conditions + a treatment."""
    shutil.copytree(suite_dir, tmp_path / "suite")
    raw = yaml.safe_load((tmp_path / "suite" / "conditions.yaml").read_text())
    treatment = next(c for c in raw["conditions"] if c["role"] == "treatment")
    return raw, treatment


def _write_conditions(tmp_path: Path, raw: dict[str, Any]) -> None:
    (tmp_path / "suite" / "conditions.yaml").write_text(yaml.safe_dump(raw))


@pytest.mark.parametrize("control_of", [None, "no_such_condition"])
def test_load_suite_rejects_treatment_without_valid_control(
    suite_dir: Path, tmp_path: Path, control_of: str | None
) -> None:
    raw, treatment = _copy_suite_with_edit(suite_dir, tmp_path)
    treatment["control_of"] = control_of
    _write_conditions(tmp_path, raw)

    with pytest.raises(ValueError, match=treatment["id"]):
        load_suite(tmp_path / "suite")


def test_load_suite_rejects_control_from_other_identity(
    suite_dir: Path, tmp_path: Path
) -> None:
    raw, treatment = _copy_suite_with_edit(suite_dir, tmp_path)
    other = next(
        c
        for c in raw["conditions"]
        if c["role"] == "control" and c["identity"] != treatment["identity"]
    )
    treatment["control_of"] = other["id"]
    _write_conditions(tmp_path, raw)

    with pytest.raises(ValueError, match="different identity"):
        load_suite(tmp_path / "suite")


def test_load_suite_rejects_unknown_role(suite_dir: Path, tmp_path: Path) -> None:
    raw, treatment = _copy_suite_with_edit(suite_dir, tmp_path)
    treatment["role"] = "treatement"
    _write_conditions(tmp_path, raw)

    with pytest.raises(ValueError, match="treatement"):
        load_suite(tmp_path / "suite")


def test_assemble_prompt_separates_stem_and_choices_with_newline(
    suite_dir: Path,
) -> None:
    config = load_suite(suite_dir)
    question = config.questions[0]
    prompt = config.assemble_prompt("base_none", question)

    assert prompt.endswith("\n".join(question.choices))
    # Bug #3 fix: the stem and the first choice must not be glued together.
    stem_and_choices_boundary = prompt.index(question.choices[0])
    assert prompt[stem_and_choices_boundary - 1] == "\n"


def test_assemble_prompt_omits_stereotype_fragment_when_none(suite_dir: Path) -> None:
    config = load_suite(suite_dir)
    question = config.questions[0]
    prompt = config.assemble_prompt("base_none", question)
    identity_text = config.identities["base"].text
    # No stereotype text should appear between the identity and the question.
    assert prompt.startswith((identity_text + "\n" + question.question, identity_text))


def test_expand_jobs_produces_full_cartesian_product(suite_dir: Path) -> None:
    config = load_suite(suite_dir)
    jobs = expand_jobs(config, ["model-a"], n_samples=3)
    assert len(jobs) == 1 * 12 * 31 * 3


def test_expand_jobs_seeds_are_deterministic_and_stable_across_calls(
    suite_dir: Path,
) -> None:
    config = load_suite(suite_dir)
    jobs_1 = expand_jobs(config, ["model-a"], n_samples=2)
    jobs_2 = expand_jobs(config, ["model-a"], n_samples=2)
    assert [j.seed for j in jobs_1] == [j.seed for j in jobs_2]


def test_expand_jobs_seeds_differ_across_samples(suite_dir: Path) -> None:
    config = load_suite(suite_dir)
    jobs = expand_jobs(config, ["model-a"], n_samples=5)
    condition_question_jobs = [
        j for j in jobs if j.condition_id == "base_none" and j.question_id == "q01"
    ]
    seeds = {j.seed for j in condition_question_jobs}
    assert len(seeds) == 5  # every sample_idx gets a distinct seed
