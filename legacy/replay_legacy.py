"""Reproduce the exact buggy legacy code path for one condition.

This is NOT part of the harness. It exists only to demonstrate, on demand,
that the historical README numbers came from legacy/utils.py's inverted
`do_sample = temperature == 0.01` line (legacy/BUGS.md bug #1) — not to
recommend using this code going forward. harness/ never reproduces this
bug; see harness/backends/hf_local.py for the corrected form.

Run from the repo root — legacy/utils.py loads questions.json with a
relative path ("./questions.json"), matching how the original notebooks
always ran with the repo root as the working directory:

    python legacy/replay_legacy.py --model google/gemma-2-2b-it --condition base_none

Requires the model weights and hardware to load Gemma-2 locally
(requirements-model.txt) — this repo's CI/dev container has neither; run
this on a machine that does (e.g. the author's M4 Max, or Colab).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# legacy/utils.py does `from prompts import *`, a plain same-directory
# import exactly as it worked in Colab (prompts.py and utils.py were
# siblings in the notebook's working directory). Put legacy/ on sys.path
# so that import resolves the same way, rather than rewriting utils.py.
sys.path.insert(0, str(Path(__file__).resolve().parent))

CONDITIONS = {
    "base_none": lambda p: p.BASE_PROMPT,
    "base_mild": lambda p: p.BASE_PROMPT + "\n" + p.BASE_STEREOTYPE,
    "black_none": lambda p: p.BLACK_WOMEN_PROMPT,
    "black_severe": lambda p: p.BLACK_WOMEN_PROMPT + "\n" + p.EXTENDED_BLACK_STEREOTYPE,
    "asian_none": lambda p: p.ASIAN_PROMPT,
    "asian_positive": lambda p: p.ASIAN_PROMPT + "\n" + p.ASIAN_STEREOTYPE,
    # The reverse-negative control arm exactly as notebook cell 17 ran it
    # — legacy/BUGS.md bug #2: a no-op .replace() against BASE_PROMPT, not
    # the intended ASIAN_PROMPT + neutralized stereotype. This is *not*
    # what suites/stereotype_threat/conditions.yaml's
    # control_reverse_negative encodes.
    "control_reverse_negative_as_actually_run": lambda p: (
        p.BASE_PROMPT
        + "\n"
        + p.EXTENDED_BLACK_STEREOTYPE.replace("Asianss", "subjects")
    ),
}


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--model", default="google/gemma-2-2b-it")
    parser.add_argument("--condition", choices=sorted(CONDITIONS), default="base_none")
    parser.add_argument(
        "--repeats", type=int, default=20, help="matches the notebooks' range(20)"
    )
    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parent.parent
    if Path.cwd().resolve() != repo_root:
        print(
            f"warning: run this from {repo_root} so legacy/utils.py "
            f"finds ./questions.json",
            file=sys.stderr,
        )

    import prompts  # legacy/prompts.py, via the sys.path insert above
    import utils  # legacy/utils.py, via the sys.path insert above
    from transformers import pipeline

    pipe = pipeline(
        "text-generation", model=args.model, device_map="auto", max_new_tokens=50
    )
    prompt = CONDITIONS[args.condition](prompts)

    # temperature=1 is what every "main" run in the notebooks passed.
    # Because of bug #1, do_sample evaluates to False here — greedy
    # decoding — so every one of these `repeats` calls is expected to
    # return the identical score, not `repeats` independent samples.
    scores = [
        utils.get_model_score(pipe, prompt, temperature=1) for _ in range(args.repeats)
    ]

    print(f"condition: {args.condition}")
    print(f"scores across {args.repeats} 'repeats': {scores}")
    print(
        f"all identical (proves greedy decoding, not sampling): {len(set(scores)) == 1}"
    )


if __name__ == "__main__":
    main()
