CLAUDE.md
Guidance for Claude Code when working in this repository.
What this repo is
`LLM-Stereotypes` began as a research experiment (early 2025): testing whether stereotype-threat framing — a well-documented human psychology effect (Steele & Aronson 1995; Shih, Pittinsky & Ambady 1999) — measurably shifts the accuracy of small open-source LLMs (Gemma-2 2B / 9B) on SAT questions, across identity prompts, positive/negative stereotype conditions, and a reverse-control arm.
The science was honest but under-powered (31 questions, single sample per condition). The engineering was notebook-shaped (two near-duplicate notebooks, one per model).
What it is becoming
A general-purpose prompt-variant evaluation harness, with the stereotype experiment as its first suite. The harness is the portfolio artifact; the experiment is the demo that proves it works and gives it a memorable story.
Separation of concerns is the whole point:

* `harness/` — knows nothing about stereotypes. Runs (suite × models × conditions × questions × n_samples), caches responses, writes run artifacts, computes statistics.
* `suites/stereotype_threat/` — YAML/JSON configs defining the identity prompts, stereotype conditions, control arms, and the question bank.

If a second suite (e.g., "system-prompt A/B for a summarization task") can be added by writing configs only — no harness code changes — the design is right.
What's special about a "generic" harness?
Nothing about it is novel as a product — generic eval tools exist (lm-evaluation-harness, promptfoo, Inspect). Do not pretend otherwise, and do not reinvent their scope. The differentiation, which the design must protect:

1. It runs controlled experiments, not benchmarks. Benchmark tools answer "what is the score." This harness answers "does manipulation X change behavior relative to a control, with what confidence." Its first-class objects are condition contrasts: paired per-question comparison across arms (McNemar), designated control arms, effect sizes with CIs, multiple-comparison correction, power analysis. Existing tools treat conditions as separate runs and leave the statistics to the user — here the statistics ARE the product. Every design decision should serve this (e.g., `stats.py` compares arms, it doesn't just score them; `compare` works on any two runs).
2. Small enough to own end-to-end. No framework dependencies; readable in one sitting. For the portfolio that means the author can defend every line; for production it's the honest pitch — an eval gate a team fully owns beats one it merely configures.
3. The repo's real differentiator is harness + suite together. The psychology-grounded suite makes it memorable and proves the harness on a genuinely tricky experimental design; the harness makes the skills transferable. Neither half carries the repo alone.

The interview answer to "why not lm-eval-harness?": "I'm not measuring capability, I'm measuring a causal contrast between prompt conditions — the experimental-design statistics are the point, and no benchmark tool gives me paired condition comparison with controls out of the box."
Prime directive: interview defensibility
The author must be able to defend every line of this repo in a technical interview without hedging. Therefore:

1. Never fabricate, extrapolate, or beautify results. Report what runs produced. If a run wasn't executed, the README says so.
2. Never narrate noise as signal. A delta smaller than its confidence interval is reported as "not distinguishable from noise," full stop. The original README called 1-question deltas "improved"/"deteriorated" — the rewrite must not repeat this.
3. Keep the original notebooks. Move them to `legacy/` untouched. They are the honest "before" of the engineering story, and the commit history showing notebook → harness is itself portfolio content.
4. Prefer a smaller claim that is airtight over a bigger claim that isn't. The strongest headline available is likely: "no effect detectable at this power; here is the exact N required" — that is a good result. Do not manufacture a positive finding.

Environment constraints

* Primary dev machine is Apple Silicon (M4 Max, 32-core GPU, Metal 4). Local inference IS runnable: PyTorch MPS via `transformers` covers the Gemma-2 2B/9B reproduction runs; prefer MLX or llama.cpp (quantized) for anything larger. Detect the device at runtime (MPS → CUDA → CPU) — never hardcode a device or assume CUDA.
* Gemma-2 (pinned revisions) stays the baseline pair — do not swap it for Gemma 4. It is the only place historical results exist, so Phase 3's reproduction claim depends on it, and it is where the effect (such as it is) was observed — newer, more heavily bias-mitigated generations would likely start from a flat null with nothing to contrast. Gemma 4 enters in Phase 4 as the generation-comparison axis, not as the replacement baseline.
* vLLM is optional, and enters as an endpoint, not a dependency. The workload is small-batch offline generation (~10K short completions of small models), not high-throughput serving, so vLLM is never required. If the author wants it (vllm-metal on the Mac, or vLLM proper on CUDA in Colab/cloud), run it as a local OpenAI-compatible server and point the `api.py` backend at its base URL — the harness core stays vLLM-free. This preserves the no-heavyweight-deps rule and doubles as proof that the backend protocol is inference-engine-agnostic (transformers/MPS, llama.cpp, and a vLLM server all behind the same interface).
* Historical results were produced on CUDA (Colab T4). MPS numerics can differ slightly from CUDA, so record device/backend in every run manifest and treat small cross-device deltas (within the CIs) as reproduction, not regression.
* Tests and CI remain GPU-free: everything under `tests/` runs against `MockBackend` (deterministic fake model) with no model weights and no network. API-model runs need only network + keys.
* Secrets via env vars only (`HF_TOKEN`, `ANTHROPIC_API_KEY`, etc.). Never commit keys; never print them.
* Python 3.11+. Use `uv` if available, else pip + a pinned `requirements.txt` (runtime) and `requirements-dev.txt` (lint/test).

Target architecture

```
harness/
  backends/        # ModelBackend protocol: generate(prompt, *, seed, temperature) -> str
    hf_local.py    #   transformers, pinned model revision (keep existing commit hashes); device auto-select mps/cuda/cpu
    api.py         #   Anthropic / OpenAI / Gemini via one thin adapter
    mock.py        #   deterministic; used by all tests and CI
  runner.py        # expands suite config -> jobs; executes with resume support
  cache.py         # (backend, model_rev, prompt_hash, question_id, sample_idx, seed) -> response; sqlite or on-disk JSON
  scoring.py       # answer extraction + exact-match scoring; extraction failures are a tracked category, not silently wrong
  stats.py         # per-condition accuracy, bootstrap 95% CIs, pairwise two-proportion / McNemar,
                   # multiple-comparison correction (Holm), power analysis (required N for target effect)
  manifest.py      # run manifest: git SHA, config hash, model revision, seed, timestamp, package versions, device/backend
  report.py        # results/<run_id>/ -> markdown summary + plots with error bars
suites/
  stereotype_threat/
    conditions.yaml    # identity prompts × stereotype framings × controls (ported verbatim from prompts.py)
    questions.json     # existing bank, plus provenance metadata
data/
  fetch_sat.py     # downloads SAT practice PDFs from College Board; PDFs themselves are NOT committed
legacy/            # original notebooks, untouched
tests/             # pytest against MockBackend; no network, no GPU
results/           # committed run artifacts for the headline runs only (small JSON/CSV + plots)

```

CLI: `python -m harness run suites/stereotype_threat --models gemma-2b,gemma-9b --n-samples 25` and `python -m harness report results/<run_id>` and `python -m harness compare results/<run_a> results/<run_b>`.
Phased plan (work in this order; one PR-sized commit series per phase)
Phase 0 — Hygiene (do first, ~minutes)

* Remove `.DS_Store`; add it to `.gitignore` along with `results/cache/`, `__pycache__/`, `.env`.
* Remove both SAT PDFs from the repo (College Board copyright — link, don't redistribute). Add `data/fetch_sat.py` + README instructions. Note: rewriting git history to purge the PDFs is optional; removing them going forward is sufficient.
* Fix the README's setup step that says "access token from databricks" — it means Hugging Face.
* Remind the author (in your summary, since this needs the GitHub UI): the repo's About/description field says "Experiment on Go" — change it to e.g. "Evaluation harness for prompt-variant experiments; first suite: stereotype threat in small LLMs."

Phase 1 — Extract the harness

* Port logic out of the notebooks into `harness/` + `suites/stereotype_threat/`. The two model notebooks collapse into one parameterized runner.
* Prompts move verbatim into `conditions.yaml`. Diff them against `prompts.py` to guarantee fidelity — the historical results must remain reproducible from the new configs.
* `MockBackend` + tests for: config expansion, prompt assembly, answer extraction, cache round-trip, manifest writing.

Phase 2 — Variance and statistics (highest scientific value)

* Repeated sampling: default `n_samples=25` per (condition × question) at temperature 0.7, seeded where the backend allows.
* `stats.py` as specified above. Every reported number carries a CI. Pairwise comparisons get Holm correction — with ~12 conditions, uncorrected p-values are indefensible.
* Power analysis output: "to detect a Δ of X points at α=.05, power .8, you need N questions" — formalizing the calculation already sketched in the old README.

Phase 3 — Honest re-run and README rewrite

* Author re-runs the suite locally on the M4 Max (MPS) through the new harness — Colab only as a fallback; commit the run artifacts for the headline runs.
* Rewrite README with this structure: the story (psychology → LLMs) → harness design (with a small architecture diagram) → how to add a new suite in config-only → results with error bars → limitations, stated first-person and prominently:
   * Training-data contamination: SAT practice tests are almost certainly in Gemma's training corpus. Say so, and say why within-condition comparison survives it (same questions across all arms) while absolute scores don't mean much.
   * Small question bank; per-question independence (not a "test session"); two models from one family.
* Keep the ethics section; it's good. Add one line noting this is the same category of work as published bias/safety evals (Malberg et al. is already cited).

Phase 4 — Stretch (only after 0–3 are done)

* Within-family generation axis: run the same suite on newer Gemma generations — Gemma 3 (Mar 2025) and Gemma 4 (Apr 2026, Apache 2.0; sizes E2B/E4B/12B/26B-MoE/31B, the small and mid sizes run comfortably on the M4 Max, quantized for the largest). "Does the stereotype effect shrink across Gemma 2 → 3 → 4?" is a sharper, fully-local question than a frontier-API comparison, and it directly extends the old README's 2B-vs-9B hypothesis about bias-mitigation training.
* API backends → run the suite on 2–3 frontier models. Expect ceiling effects on SAT; a saturated model is a reportable finding ("effect vanishes with scale/RLHF"), not a failure. Optionally add a harder question set as a second data source.
* GitHub Actions CI: ruff + pytest on MockBackend. A green CI badge on a portfolio repo punches above its weight.
* A second, deliberately boring suite (e.g., two system-prompt variants on a small QA set) to prove the config-only extension claim.

Production-convertible components (the "excellent" part)
Build these as if they'll be lifted into a real codebase, because they can be — this is the strongest interview material in the repo:

1. The harness itself is a prompt-regression / model-upgrade gate. "Does swapping model A for model B (or prompt v1 for v2) change task accuracy, with statistical gating instead of vibes?" is a real production need at most LLM shops. `compare` between two run artifacts, with CIs and a pass/fail threshold, is exactly that tool. Name this in the README.
2. Run manifests + artifact layout = experiment tracking discipline (the MLflow habit, dependency-free).
3. Response cache with resume = the cost/idempotency pattern of any production batch-inference pipeline.
4. Scored answer-extraction with a tracked failure category = the unsexy 80% of every real eval system.
5. Config-defined suites = how production eval sets are actually managed (versioned, reviewable, no code changes to add cases).

When writing the README's harness section, frame it in these terms — evaluation infrastructure — not as "my bias experiment's helper code."
Conventions

* Type hints everywhere; `ruff` for lint+format; `pytest` for tests.
* Small, single-purpose commits with imperative messages ("Extract prompt assembly into harness", not "updates"). The commit history will be read by humans.
* No notebook in the core execution path. One `analysis.ipynb` may exist for plots, consuming `results/` artifacts only.
* Docstrings explain why (design intent), not what.

Do not

* Do not expand the science (more questions, more models) before Phases 0–2 exist. Scale is a config change once the harness is real — that ordering is the point.
* Do not invent, simulate, or "estimate" model outputs to fill result tables. Result tables come only from executed runs; if a run hasn't happened yet, leave a TODO marker.
* Do not soften or delete the limitations section to make results look better.
* Do not add heavyweight dependencies (LangChain, MLflow, W&B). Part of the demonstration is that clean eval infrastructure is small.
* Do not remove the psychology citations or the ethics discussion — the cross-domain story is the differentiator.
