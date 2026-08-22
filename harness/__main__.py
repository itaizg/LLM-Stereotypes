"""CLI entry point:

python -m harness run suites/stereotype_threat --models gemma-2b,gemma-9b --n-samples 25
python -m harness report results/<run_id>
python -m harness compare results/<run_a> results/<run_b>
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from harness.config import load_suite
from harness.models import MODEL_REGISTRY
from harness.report import compare_runs, write_report
from harness.runner import run_suite


def _build_backend(backend_name: str, model_label: str):
    if backend_name == "hf_local":
        from harness.backends.hf_local import HFLocalBackend

        spec = MODEL_REGISTRY.get(model_label)
        if spec is None:
            raise SystemExit(
                f"unknown model label {model_label!r}; known labels: {sorted(MODEL_REGISTRY)}"
            )
        return HFLocalBackend(
            spec["model_id"],
            revision=spec["revision"],
            max_new_tokens=spec.get("max_new_tokens", 50),
        )
    if backend_name == "api":
        from harness.backends.api import ApiBackend

        return ApiBackend(model_label)
    raise SystemExit(f"unknown backend: {backend_name}")


def cmd_run(args: argparse.Namespace) -> None:
    config = load_suite(args.suite_dir)
    models = args.models.split(",")

    for model_label in models:
        if args.backend == "mock":
            from harness.backends.mock import MockBackend

            gold_answers = {q.question_id: q.answer for q in config.questions}
            backend = MockBackend(
                gold_answers, target_accuracy=args.mock_target_accuracy
            )
        else:
            backend = _build_backend(args.backend, model_label)

        run_dir = run_suite(
            config,
            backend,
            model_label=model_label,
            n_samples=args.n_samples,
            temperature=args.temperature,
            results_dir=args.results_dir,
        )
        print(f"wrote {run_dir}")

        if not args.no_report:
            report_path = write_report(run_dir, suites_root=Path(args.suite_dir).parent)
            print(f"wrote {report_path}")


def cmd_report(args: argparse.Namespace) -> None:
    report_path = write_report(args.run_dir, suites_root=args.suites_root)
    print(f"wrote {report_path}")


def cmd_compare(args: argparse.Namespace) -> None:
    comparison = compare_runs(args.run_a, args.run_b, suites_root=args.suites_root)
    print(json.dumps(comparison, indent=2))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m harness")
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run", help="run a suite against a backend")
    run_parser.add_argument("suite_dir", help="e.g. suites/stereotype_threat")
    run_parser.add_argument(
        "--models", required=True, help="comma-separated model labels"
    )
    run_parser.add_argument(
        "--backend", default="hf_local", choices=["hf_local", "mock", "api"]
    )
    run_parser.add_argument("--n-samples", type=int, default=None)
    run_parser.add_argument("--temperature", type=float, default=None)
    run_parser.add_argument("--results-dir", default="results")
    run_parser.add_argument("--mock-target-accuracy", type=float, default=0.6)
    run_parser.add_argument("--no-report", action="store_true")
    run_parser.set_defaults(func=cmd_run)

    report_parser = subparsers.add_parser(
        "report", help="render stats.json + report.md (+ plots) for a run"
    )
    report_parser.add_argument("run_dir")
    report_parser.add_argument("--suites-root", default="suites")
    report_parser.set_defaults(func=cmd_report)

    compare_parser = subparsers.add_parser(
        "compare", help="contrast the same condition across two run artifacts"
    )
    compare_parser.add_argument("run_a")
    compare_parser.add_argument("run_b")
    compare_parser.add_argument("--suites-root", default="suites")
    compare_parser.set_defaults(func=cmd_compare)

    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
