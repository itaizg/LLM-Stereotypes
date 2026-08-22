import json

from harness.backends.base import BackendDescriptor
from harness.manifest import build_manifest


def test_manifest_has_required_fields(tmp_path):
    descriptor = BackendDescriptor(
        name="mock", model_id="mock-deterministic", device="cpu"
    )
    manifest = build_manifest(
        run_id="run123",
        suite="stereotype_threat",
        config_hash="abcd1234",
        backend_descriptor=descriptor,
        n_samples=5,
        temperature=0.7,
    )

    assert manifest.run_id == "run123"
    assert manifest.suite == "stereotype_threat"
    assert manifest.config_hash == "abcd1234"
    assert manifest.backend == descriptor
    assert manifest.n_samples == 5
    assert manifest.temperature == 0.7
    assert manifest.git_sha  # non-empty, even if "unknown"
    assert isinstance(manifest.git_dirty, bool)
    assert manifest.timestamp
    assert isinstance(manifest.package_versions, dict)
    assert manifest.python_version


def test_manifest_write_round_trips_to_json(tmp_path):
    descriptor = BackendDescriptor(
        name="hf_local",
        model_id="google/gemma-2-2b-it",
        revision="abc123",
        device="mps",
    )
    manifest = build_manifest(
        run_id="run456",
        suite="stereotype_threat",
        config_hash="deadbeef",
        backend_descriptor=descriptor,
        n_samples=25,
        temperature=0.7,
    )
    out_path = tmp_path / "manifest.json"
    manifest.write(out_path)

    with open(out_path) as f:
        loaded = json.load(f)

    assert loaded["run_id"] == "run456"
    assert loaded["backend"]["device"] == "mps"
    assert loaded["backend"]["revision"] == "abc123"
