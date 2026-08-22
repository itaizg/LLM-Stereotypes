"""Run manifests: git SHA, config hash, model revision, seed policy,
timestamp, package versions, device/backend — the dependency-free version
of the MLflow experiment-tracking habit.

device/backend is required rather than optional: historical results were
produced on CUDA (Colab T4/A100), MPS numerics can differ slightly, and a
manifest that doesn't record which one produced a run makes "is this
reproduction or regression" unanswerable later.
"""

from __future__ import annotations

import importlib.metadata
import json
import subprocess
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from harness.backends.base import BackendDescriptor


def _run_git(*args: str) -> str:
    try:
        return subprocess.check_output(
            ["git", *args], stderr=subprocess.DEVNULL, cwd=Path(__file__).resolve().parent
        ).decode().strip()
    except Exception:
        return ""


def _git_sha() -> str:
    return _run_git("rev-parse", "HEAD") or "unknown"


def _git_dirty() -> bool:
    return bool(_run_git("status", "--porcelain"))


def _package_versions(names: tuple[str, ...]) -> dict[str, str]:
    versions = {}
    for name in names:
        try:
            versions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            versions[name] = "not installed"
    return versions


_TRACKED_PACKAGES = ("torch", "transformers", "numpy", "scipy", "pyyaml")


@dataclass(frozen=True)
class RunManifest:
    run_id: str
    suite: str
    config_hash: str
    backend: BackendDescriptor
    n_samples: int
    temperature: float
    git_sha: str
    git_dirty: bool
    timestamp: str
    package_versions: dict[str, str]
    python_version: str

    def to_dict(self) -> dict:
        return asdict(self)

    def write(self, path: Path | str) -> None:
        with open(path, "w") as f:
            json.dump(self.to_dict(), f, indent=2)
            f.write("\n")


def build_manifest(
    *,
    run_id: str,
    suite: str,
    config_hash: str,
    backend_descriptor: BackendDescriptor,
    n_samples: int,
    temperature: float,
) -> RunManifest:
    return RunManifest(
        run_id=run_id,
        suite=suite,
        config_hash=config_hash,
        backend=backend_descriptor,
        n_samples=n_samples,
        temperature=temperature,
        git_sha=_git_sha(),
        git_dirty=_git_dirty(),
        timestamp=datetime.now(timezone.utc).isoformat(),
        package_versions=_package_versions(_TRACKED_PACKAGES),
        python_version=sys.version,
    )
