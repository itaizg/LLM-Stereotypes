# Known bugs in the legacy code

This code (`prompts.py`, `utils.py`, and the three notebooks in this
directory) is preserved untouched as the "before" of this repo's
notebook-to-harness refactor. It produced every number in the README's
results table. Three bugs in it mean those numbers do not mean what the
original README's prose claims — documented here rather than silently
fixed, so the historical results can be labeled honestly.

## 1. Sampling was inverted — the "20 repeats" are 20 identical runs

`utils.py:19`:

```python
do_sample = temperature == 0.01
```

This is backwards. The experiment notebooks call
`get_model_score(pipe, prompt, temperature=1)` twenty times per condition,
intending twenty independent stochastic samples. But `do_sample` is only
`True` when `temperature == 0.01` — the "near-zero" comparison point, not
the main run. At `temperature=1`, `do_sample` evaluates to `False`, which
makes `transformers` fall back to **greedy decoding** and ignore
`temperature` entirely.

So every "20 repeats at temperature=1" in `experiment_gemma_2b.ipynb` and
`experiment_gemma_9b.ipynb` are twenty bit-identical copies of one
deterministic greedy-decoded run. The README's scores are single-sample
results with no variance behind them, not an average over 20 samples as
the prose implies.

This also explains `temperature_comparison.ipynb`'s conclusion that
"temperature had no effect at all": both the temperature=0.01 condition and
the temperature=1 condition it was compared against were effectively
deterministic, one by design (near-zero temperature) and one by this bug.

The harness fixes this by setting `do_sample=True` whenever
`temperature > 0` (see `harness/backends/hf_local.py`), and by taking
`n_samples` independently seeded generations per (condition, question).

## 2. The "Asian negative-stereotype" control arm never ran what it says

`experiment_gemma_2b.ipynb` cell 17 and `experiment_gemma_9b.ipynb` cell 17:

```python
neg_stereotype = [get_model_score(pipe, BASE_PROMPT + "\n" + EXTENDED_BLACK_STEREOTYPE.replace("Asianss", "subjects"), temperature=1) for i in tqdm(range(20))]
```

Two problems in this one line:

- `.replace("Asianss", "subjects")` never matches — `"Asianss"` does not
  occur anywhere in `EXTENDED_BLACK_STEREOTYPE`. The call is a no-op, so the
  text sent to the model is the **unmodified, verbatim** negative
  stereotype (explicitly naming "African American women"), not a
  neutralized version.
- The prompt uses `BASE_PROMPT` ("You are a logic expert..."), not
  `ASIAN_PROMPT`. The README's "Control (reverse): Asian prompt + African
  American woman's negative stereotype" row (line 60 of the original
  README) does not correspond to what this cell ran.

What actually ran: `BASE_PROMPT` + the unmodified `EXTENDED_BLACK_STEREOTYPE`
— i.e. a duplicate of the "Base + negative stereotype (non-racial)" row,
except the stereotype text was not actually neutralized.

The harness's `suites/stereotype_threat/conditions.yaml` encodes this arm
as the *intended* condition (Asian identity + a neutralized negative
stereotype fragment), verified against the correct `.replace()` semantics
in `tests/test_prompt_fidelity.py`. It does not reproduce this bug.

## 3. Missing separator between the question stem and the first answer choice

`utils.py:11`:

```python
questions = [question_list[i]['question'] + "\n".join(question_list[i]['choices']) for i in range(len(question_list))]
```

`"\n".join(choices)` puts newlines *between* choices but not before the
first one, so the first choice (e.g. `"A) demands"`) is concatenated
directly onto the last line of the question stem with no separator, for
all 31 questions. This is a minor formatting defect (Gemma likely still
parses it) but is fixed in `harness/scoring.py`'s prompt assembly with a
proper newline before the choice block.

## Reproducing the historical numbers exactly

`legacy/replay_legacy.py` imports `legacy/utils.py` unmodified and reruns
one condition through the exact buggy path above, to demonstrate that the
historical README numbers came from this code as documented — not to
recommend using it. The harness itself (`harness/`) never reproduces these
bugs.
