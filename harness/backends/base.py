"""The interface every inference engine implements.

Keeping this surface tiny is what makes the harness backend-agnostic:
transformers/MPS, llama.cpp, and an OpenAI-compatible vLLM server all sit
behind the same three members, so swapping one in for another never touches
runner.py, cache.py, or scoring.py.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Optional, Protocol


@dataclass(frozen=True)
class BackendDescriptor:
    """Identifies exactly what produced a response, for the run manifest."""

    name: str
    model_id: str
    revision: Optional[str] = None
    device: Optional[str] = None


class ModelBackend(Protocol):
    @property
    def descriptor(self) -> BackendDescriptor: ...

    def generate(
        self,
        prompt: str,
        *,
        seed: int,
        temperature: float,
        context: Optional[Mapping[str, str]] = None,
    ) -> str:
        """Return the raw completion text for one prompt.

        `context` carries opaque identifying metadata (e.g. question_id,
        condition_id) that a real backend ignores but MockBackend uses to
        simulate a target accuracy without parsing the prompt text.
        """
        ...
