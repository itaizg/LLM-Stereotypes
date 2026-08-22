"""Local transformers backend for Gemma-2 2B/9B (and similar chat models).

Requires requirements-model.txt (torch, transformers, accelerate) — not
imported by anything in the core test path, so `pytest` never needs a GPU
or model weights.
"""

from __future__ import annotations

from typing import Mapping, Optional

from harness.backends.base import BackendDescriptor, ModelBackend


def _select_device() -> str:
    import torch

    if torch.backends.mps.is_available():
        return "mps"
    if torch.cuda.is_available():
        return "cuda"
    return "cpu"


class HFLocalBackend(ModelBackend):
    """Detects device at runtime (MPS -> CUDA -> CPU); never hardcode one."""

    def __init__(self, model_id: str, *, revision: str, max_new_tokens: int = 50, device: Optional[str] = None) -> None:
        from transformers import pipeline

        self._model_id = model_id
        self._revision = revision
        self._device = device or _select_device()
        self._max_new_tokens = max_new_tokens
        self._pipe = pipeline(
            "text-generation",
            model=model_id,
            revision=revision,
            device=self._device,
            max_new_tokens=max_new_tokens,
        )

    @property
    def descriptor(self) -> BackendDescriptor:
        return BackendDescriptor(
            name="hf_local",
            model_id=self._model_id,
            revision=self._revision,
            device=self._device,
        )

    def generate(
        self,
        prompt: str,
        *,
        seed: int,
        temperature: float,
        context: Optional[Mapping[str, str]] = None,
    ) -> str:
        import torch

        torch.manual_seed(seed)
        messages = [{"role": "user", "content": prompt}]
        # Corrected form of the legacy do_sample bug (legacy/BUGS.md #1):
        # sampling must track temperature, not the reverse.
        do_sample = temperature > 0
        output = self._pipe(
            messages,
            temperature=temperature if do_sample else None,
            do_sample=do_sample,
        )[0]["generated_text"][-1]["content"]
        return output
