"""Known model labels for the `--models` CLI flag, resolving to the
Hugging Face model id and pinned revision.

Revisions are the commit hashes that were previously stranded only in the
README's prose (legacy/BUGS.md's context) — pinning them here is what
makes an hf_local run reproducible across machines and time.
"""

from typing import TypedDict


class ModelSpec(TypedDict):
    model_id: str
    revision: str
    max_new_tokens: int


MODEL_REGISTRY: dict[str, ModelSpec] = {
    "gemma-2b": {
        "model_id": "google/gemma-2-2b-it",
        "revision": "299a8560bedf22ed1c72a8a11e7dce4a7f9f51f8",
        "max_new_tokens": 50,
    },
    "gemma-9b": {
        "model_id": "google/gemma-2-9b-it",
        "revision": "11c9b309abf73637e4b6f9a3fa1e92e615547819",
        "max_new_tokens": 25,
    },
}
