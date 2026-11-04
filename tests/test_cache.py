from typing import Any

from harness.cache import ResponseCache


def test_cache_round_trip(tmp_path):
    with ResponseCache(tmp_path / "cache.sqlite3") as cache:
        key = ResponseCache.make_key(
            backend="mock",
            model_rev="rev1",
            prompt="hello",
            question_id="q01",
            sample_idx=0,
            seed=42,
        )
        assert cache.get(key) is None
        cache.put(key, "B")
        assert cache.get(key) == "B"


def test_cache_persists_across_connections(tmp_path):
    db_path = tmp_path / "cache.sqlite3"
    key = ResponseCache.make_key(
        backend="mock",
        model_rev="rev1",
        prompt="hello",
        question_id="q01",
        sample_idx=0,
        seed=1,
    )
    with ResponseCache(db_path) as cache:
        cache.put(key, "A")

    with ResponseCache(db_path) as cache:
        assert cache.get(key) == "A"


def test_make_key_differs_on_every_component():
    base: dict[str, Any] = {
        "backend": "mock",
        "model_rev": "rev1",
        "prompt": "p",
        "question_id": "q01",
        "sample_idx": 0,
        "seed": 1,
    }
    key = ResponseCache.make_key(**base)

    for field, new_value in [
        ("backend", "other"),
        ("model_rev", "rev2"),
        ("prompt", "different prompt"),
        ("question_id", "q02"),
        ("sample_idx", 1),
        ("seed", 2),
    ]:
        variant = dict(base)
        variant[field] = new_value
        assert ResponseCache.make_key(**variant) != key
