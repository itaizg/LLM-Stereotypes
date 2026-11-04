import shutil
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

REAL_SUITE_DIR = REPO_ROOT / "suites" / "stereotype_threat"
SYNTHETIC_QUESTIONS = (
    Path(__file__).resolve().parent / "fixtures" / "synthetic_questions.json"
)


@pytest.fixture(scope="session")
def suites_root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A suites root whose stereotype_threat suite pairs the real, tracked
    conditions.yaml with a synthetic question bank.

    The real question bank is College Board material and is not tracked, so
    tests (and CI) must never depend on it. Keeping the real conditions file
    means the prompts under test are still the ones the experiment uses.
    """
    root = tmp_path_factory.mktemp("suites")
    suite = root / "stereotype_threat"
    suite.mkdir()
    shutil.copy(REAL_SUITE_DIR / "conditions.yaml", suite / "conditions.yaml")
    shutil.copy(SYNTHETIC_QUESTIONS, suite / "questions.json")
    return root


@pytest.fixture(scope="session")
def suite_dir(suites_root: Path) -> Path:
    return suites_root / "stereotype_threat"
