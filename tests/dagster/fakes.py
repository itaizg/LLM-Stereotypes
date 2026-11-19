"""Test doubles for the orchestration tests.

Kept out of ``conftest.py`` so test modules can import them by name: a plain
``from conftest import …`` is ambiguous, because ``tests/conftest.py`` has the same
module name.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from exam_dagster.stage import Completed


@dataclass
class FakeRunner:
    """Answers stage commands from a script and records what was asked.

    ``script`` maps an argv prefix to ``(exit_code, stdout, stderr)``; the longest
    matching prefix wins, and an unscripted argv exits 0 with no output. ``then`` lists
    answers for an exact argv that are used one per call, before the ``script`` answer
    takes over: how a test says "fail twice, then succeed".
    """

    script: dict[tuple[str, ...], tuple[int, str, str]] = field(default_factory=dict)
    then: dict[tuple[str, ...], list[tuple[int, str, str]]] = field(
        default_factory=dict
    )
    commands: list[tuple[str, ...]] = field(default_factory=list)
    cwds: list[str] = field(default_factory=list)

    @property
    def argvs(self) -> list[tuple[str, ...]]:
        """Each recorded command without the fixed ``python -m pipeline`` prefix."""
        from exam_dagster.stage import command_prefix

        return [c[len(command_prefix()) :] for c in self.commands]

    def __call__(self, command: Sequence[str], *, cwd: str) -> Completed:
        from exam_dagster.stage import Completed, command_prefix

        self.commands.append(tuple(command))
        self.cwds.append(cwd)
        argv = tuple(command[len(command_prefix()) :])
        queued = self.then.get(argv)
        if queued:
            return Completed(*queued.pop(0))
        best: tuple[int, str, str] = (0, "", "")
        best_len = -1
        for prefix, answer in self.script.items():
            if argv[: len(prefix)] == prefix and len(prefix) > best_len:
                best, best_len = answer, len(prefix)
        code, out, err = best
        return Completed(code, out, err)
