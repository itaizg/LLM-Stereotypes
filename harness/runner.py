"""Expands a suite config into jobs and executes them against a backend,
writing the run artifact directory.

Resume is a property of the response cache, not of this function: rerunning
with the same cache_path skips every job whose (backend, model_rev,
prompt_hash, question_id, sample_idx, seed) key is already cached, so an
interrupted run (or a request for more samples) never re-pays for
completions it already has.
"""

from __future__ import annotations

import csv
import json
import time
from pathlib import Path

from harness.backends.base import ModelBackend
from harness.cache import ResponseCache
from harness.config import SuiteConfig, expand_jobs
from harness.manifest import build_manifest
from harness.scoring import score_response

RESPONSES_FILENAME = "responses.jsonl"
SCORES_FILENAME = "scores.csv"
MANIFEST_FILENAME = "manifest.json"


def run_suite(
    config: SuiteConfig,
    backend: ModelBackend,
    *,
    model_label: str,
    n_samples: int | None = None,
    temperature: float | None = None,
    results_dir: Path | str = "results",
    cache_path: Path | str | None = None,
) -> Path:
    n_samples = n_samples if n_samples is not None else config.n_samples_default
    temperature = temperature if temperature is not None else config.temperature_default

    jobs = expand_jobs(config, [model_label], n_samples)
    descriptor = backend.descriptor

    run_id = f"{config.suite}_{model_label}_{int(time.time())}_{config.config_hash}"
    run_dir = Path(results_dir) / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    if cache_path is None:
        cache_path = Path(results_dir) / "cache" / f"{model_label}.sqlite3"
    cache_path = Path(cache_path)
    cache_path.parent.mkdir(parents=True, exist_ok=True)

    questions_by_id = {q.question_id: q for q in config.questions}

    with (
        ResponseCache(cache_path) as cache,
        open(run_dir / RESPONSES_FILENAME, "w") as responses_f,
        open(run_dir / SCORES_FILENAME, "w", newline="") as scores_f,
    ):
        scores_writer = csv.writer(scores_f)
        scores_writer.writerow(
            [
                "condition_id",
                "question_id",
                "sample_idx",
                "extracted_answer",
                "gold_answer",
                "is_correct",
                "is_extraction_failure",
            ]
        )

        for job in jobs:
            question = questions_by_id[job.question_id]
            prompt = config.assemble_prompt(job.condition_id, question)

            cache_key = ResponseCache.make_key(
                backend=descriptor.name,
                model_rev=descriptor.revision or descriptor.model_id,
                prompt=prompt,
                question_id=job.question_id,
                sample_idx=job.sample_idx,
                seed=job.seed,
            )

            raw_response = cache.get(cache_key)
            if raw_response is None:
                raw_response = backend.generate(
                    prompt,
                    seed=job.seed,
                    temperature=temperature,
                    context={
                        "question_id": job.question_id,
                        "condition_id": job.condition_id,
                    },
                )
                cache.put(cache_key, raw_response)

            scored = score_response(raw_response, question.answer)

            responses_f.write(
                json.dumps(
                    {
                        "condition_id": job.condition_id,
                        "question_id": job.question_id,
                        "sample_idx": job.sample_idx,
                        "seed": job.seed,
                        "raw_response": raw_response,
                    }
                )
                + "\n"
            )

            scores_writer.writerow(
                [
                    job.condition_id,
                    job.question_id,
                    job.sample_idx,
                    scored.extracted_answer or "",
                    question.answer,
                    int(scored.is_correct),
                    int(scored.is_extraction_failure),
                ]
            )

    manifest = build_manifest(
        run_id=run_id,
        suite=config.suite,
        config_hash=config.config_hash,
        backend_descriptor=descriptor,
        n_samples=n_samples,
        temperature=temperature,
    )
    manifest.write(run_dir / MANIFEST_FILENAME)

    return run_dir
