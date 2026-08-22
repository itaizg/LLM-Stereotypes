"""Guards suites/stereotype_threat/conditions.yaml against silent drift from
legacy/prompts.py — the historical results only stay meaningful as a
reference point if the ported prompts are provably identical to (or, for
the two control-reverse arms, provably the *intended* fix of) the
originals. See legacy/BUGS.md for the bug this test deliberately does not
reproduce.
"""

from pathlib import Path

import yaml

from legacy import prompts

CONDITIONS_PATH = (
    Path(__file__).resolve().parents[1] / "suites" / "stereotype_threat" / "conditions.yaml"
)


def _load_conditions() -> dict:
    with open(CONDITIONS_PATH) as f:
        return yaml.safe_load(f)


def test_identity_prompts_match_legacy_verbatim():
    config = _load_conditions()
    assert config["identities"]["base"]["text"] == prompts.BASE_PROMPT
    assert config["identities"]["black_women"]["text"] == prompts.BLACK_WOMEN_PROMPT
    assert config["identities"]["asian"]["text"] == prompts.ASIAN_PROMPT


def test_stereotype_fragments_match_legacy_verbatim():
    config = _load_conditions()
    assert config["stereotypes"]["mild"]["text"] == prompts.BASE_STEREOTYPE
    assert config["stereotypes"]["black_severe"]["text"] == prompts.EXTENDED_BLACK_STEREOTYPE
    assert config["stereotypes"]["asian_positive"]["text"] == prompts.ASIAN_STEREOTYPE


def test_base_positive_nonracial_matches_legacy_replace():
    config = _load_conditions()
    expected = prompts.ASIAN_STEREOTYPE.replace("Asian", "subject")
    assert config["stereotypes"]["base_positive_nonracial"]["text"] == expected


def test_base_negative_nonracial_matches_legacy_replace():
    config = _load_conditions()
    expected = prompts.EXTENDED_BLACK_STEREOTYPE.replace("African American women", "subjects")
    assert config["stereotypes"]["base_negative_nonracial"]["text"] == expected


def test_control_reverse_positive_matches_legacy_replace_chain():
    config = _load_conditions()
    expected = prompts.ASIAN_STEREOTYPE.replace("Asians", "African American women").replace(
        "Asian", "African American woman"
    )
    assert config["stereotypes"]["control_reverse_positive"]["text"] == expected


def test_control_reverse_negative_matches_intended_fix_not_the_bug():
    config = _load_conditions()
    intended = prompts.EXTENDED_BLACK_STEREOTYPE.replace("African American women", "Asians")
    buggy_noop = prompts.EXTENDED_BLACK_STEREOTYPE.replace("Asianss", "subjects")

    # legacy/BUGS.md bug #2: "Asianss" never occurs, so the buggy .replace()
    # call is provably a no-op — confirm that premise so this test would
    # fail loudly if legacy/prompts.py ever changed underneath it.
    assert buggy_noop == prompts.EXTENDED_BLACK_STEREOTYPE

    assert config["stereotypes"]["control_reverse_negative"]["text"] == intended
    assert config["stereotypes"]["control_reverse_negative"]["text"] != buggy_noop


def test_all_conditions_reference_declared_identities_and_stereotypes():
    config = _load_conditions()
    identity_ids = set(config["identities"])
    stereotype_ids = set(config["stereotypes"])

    assert len(config["conditions"]) == 12

    for condition in config["conditions"]:
        assert condition["identity"] in identity_ids
        assert condition["stereotype"] in stereotype_ids
        if condition["role"] == "treatment":
            assert "control_of" in condition
