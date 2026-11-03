import shutil
from pathlib import Path

import pytest
import yaml

from harness.config import expand_jobs, load_suite

SUITE_DIR = Path(__file__).resolve().parent.parent / "suites" / "stereotype_threat"


def test_load_suite_has_12_conditions_and_31_questions():
    config = load_suite(SUITE_DIR)
    assert len(config.conditions) == 12
    assert len(config.questions) == 31


def test_load_suite_control_conditions_have_no_control_of():
    config = load_suite(SUITE_DIR)
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


@pytest.mark.parametrize("control_of", [None, "no_such_condition"])
def test_load_suite_rejects_treatment_without_valid_control(tmp_path, control_of):
    suite_dir = tmp_path / "suite"
    shutil.copytree(SUITE_DIR, suite_dir)
    conditions_path = suite_dir / "conditions.yaml"
    raw = yaml.safe_load(conditions_path.read_text())
    treatment = next(c for c in raw["conditions"] if c["role"] == "treatment")
    treatment["control_of"] = control_of
    conditions_path.write_text(yaml.safe_dump(raw))

    with pytest.raises(ValueError, match=treatment["id"]):
        load_suite(suite_dir)


def test_assemble_prompt_separates_stem_and_choices_with_newline():
    config = load_suite(SUITE_DIR)
    question = config.questions[0]
    prompt = config.assemble_prompt("base_none", question)

    assert prompt.endswith("\n".join(question.choices))
    # Bug #3 fix: the stem and the first choice must not be glued together.
    stem_and_choices_boundary = prompt.index(question.choices[0])
    assert prompt[stem_and_choices_boundary - 1] == "\n"


def test_assemble_prompt_omits_stereotype_fragment_when_none():
    config = load_suite(SUITE_DIR)
    question = config.questions[0]
    prompt = config.assemble_prompt("base_none", question)
    identity_text = config.identities["base"].text
    # No stereotype text should appear between the identity and the question.
    assert prompt.startswith((identity_text + "\n" + question.question, identity_text))


def test_expand_jobs_produces_full_cartesian_product():
    config = load_suite(SUITE_DIR)
    jobs = expand_jobs(config, ["model-a"], n_samples=3)
    assert len(jobs) == 1 * 12 * 31 * 3


def test_expand_jobs_seeds_are_deterministic_and_stable_across_calls():
    config = load_suite(SUITE_DIR)
    jobs_1 = expand_jobs(config, ["model-a"], n_samples=2)
    jobs_2 = expand_jobs(config, ["model-a"], n_samples=2)
    assert [j.seed for j in jobs_1] == [j.seed for j in jobs_2]


def test_expand_jobs_seeds_differ_across_samples():
    config = load_suite(SUITE_DIR)
    jobs = expand_jobs(config, ["model-a"], n_samples=5)
    condition_question_jobs = [
        j for j in jobs if j.condition_id == "base_none" and j.question_id == "q01"
    ]
    seeds = {j.seed for j in condition_question_jobs}
    assert len(seeds) == 5  # every sample_idx gets a distinct seed
