"""Run configuration and every threshold the crawler uses (the single home of numbers).

Defaults come from the spec and the brief: a run stops at 500 queries, $10 of LLM spend
or 100 agent iterations (FR-002); requests to one host are 5 s apart (FR-010); sampling
reads at most 2 MB or 3 PDF pages (FR-017). The maintainer approves the budget at the
start of each run (FR-001); a config file may override any value before that prompt.
"""

from __future__ import annotations

import re
import secrets
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

from crawler.contracts import Provider, ReasonCode

RUN_ID_PATTERN = re.compile(r"^cr_\d{8}T\d{6}_[0-9a-f]{4}$")

# --- validation thresholds (FR-014, FR-017, FR-018) ----------------------------------
MIN_ITEMS = 5
MIN_CHOICES = 3
MAX_CHOICES = 5
MIN_PAGE_TYPE_CONFIDENCE = 0.7
MIN_LANGUAGE_CONFIDENCE = 0.8
SAMPLE_MAX_BYTES = 2 * 1024 * 1024
SAMPLE_MAX_PDF_PAGES = 3
PROBE_MAX_BYTES = 64 * 1024
TERMS_MAX_BYTES = 256 * 1024
MAX_REDIRECTS = 5

# --- re-check policy (FR-016a) and memory retention (US4) -----------------------------
RECHECK_AFTER_DAYS = 7
MAX_TRANSIENT_ATTEMPTS = 3
DEAD_HOST_DAYS = 30

# --- per-tool limits (FR-004, contracts/tools.md) -----------------------------------
SEARCH_MAX_RESULTS = 20
SEARCH_TIMEOUT_S = 10.0
SEARCH_RETRIES = 2
PROBE_TIMEOUT_S = 10.0
PROBE_RETRIES = 2
SAMPLE_TIMEOUT_S = 30.0
SAMPLE_RETRIES = 1
TERMS_TIMEOUT_S = 10.0
TERMS_RETRIES = 1
WRITE_TIMEOUT_S = 5.0
WRITE_RETRIES = 3
MEMORY_TIMEOUT_S = 5.0
MEMORY_RETRIES = 1
MEMORY_MAX_BYTES = 1024 * 1024

# --- context management (constitution "Context management", research R5) -------------
CONTEXT_WINDOW_TOKENS = 1_000_000
COMPACT_AT_SHARE = 0.5
TOOL_OUTPUT_MAX_CHARS = 4_000

# FR-016a: each failure reason is transient (re-checked later) or permanent.
REASON_CLASS: dict[ReasonCode, Literal["transient", "permanent"]] = {
    ReasonCode.BLOCKED: "permanent",
    ReasonCode.LOGIN_WALL: "permanent",
    ReasonCode.PAYWALL: "permanent",
    ReasonCode.CAPTCHA: "permanent",
    ReasonCode.NON_PUBLIC_ADDRESS: "permanent",
    ReasonCode.BAD_SCHEME: "permanent",
    ReasonCode.TOO_MANY_REDIRECTS: "permanent",
    ReasonCode.HTTP_4XX: "permanent",
    ReasonCode.TYPE_MISMATCH: "permanent",
    ReasonCode.SCRIPT_ONLY: "permanent",
    ReasonCode.NOT_MCQ_STRUCTURE: "permanent",
    ReasonCode.NOT_MCQ_PAGE_TYPE: "permanent",
    ReasonCode.NO_INSTRUCTION_PHRASE: "permanent",
    ReasonCode.NOT_PROBED: "permanent",
    ReasonCode.TIMEOUT: "transient",
    ReasonCode.HTTP_5XX: "transient",
    ReasonCode.CONNECTION_ERROR: "transient",
    ReasonCode.TOO_LARGE_UNSAMPLED: "transient",
    ReasonCode.NOT_CLASSIFIED: "transient",
}


def reason_class(reason: ReasonCode) -> Literal["transient", "permanent"]:
    return REASON_CLASS[reason]


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Budget(Settings):
    max_queries: int = Field(default=500, ge=1)
    max_cost_usd: float = Field(default=10.0, gt=0, json_schema_extra={"unit": "USD"})
    max_iterations: int = Field(default=100, ge=1)
    max_consecutive_failures: int = Field(default=5, ge=1)
    max_tool_calls_per_iteration: int = Field(default=20, ge=1)


class ModelSpec(Settings):
    """A Claude model and its prices. Prices are config, never hard-coded in logic."""

    model: str
    price_in_usd_per_mtok: float = Field(ge=0, json_schema_extra={"unit": "USD/Mtok"})
    price_out_usd_per_mtok: float = Field(ge=0, json_schema_extra={"unit": "USD/Mtok"})
    max_output_tokens: int = Field(ge=1)

    def cost_usd(self, input_tokens: int, output_tokens: int) -> float:
        return (
            input_tokens * self.price_in_usd_per_mtok
            + output_tokens * self.price_out_usd_per_mtok
        ) / 1_000_000

    def worst_case_usd(self, input_tokens: int) -> float:
        """The most one call can cost: its input plus the full output allowance."""
        return self.cost_usd(input_tokens, self.max_output_tokens)


# First-party API prices as of 2026-11 (USD per million tokens). They are config: the
# maintainer sees them at the budget prompt and may override them in a run config.
# Output tokens include thinking, which is always on for these models.
DEFAULT_AGENT_MODEL = ModelSpec(
    model="claude-opus-5-5",
    price_in_usd_per_mtok=4.0,
    price_out_usd_per_mtok=20.0,
    max_output_tokens=4_096,
)
# The classifier sees at most a few thousand tokens, well under Haiku 5.5's 100K-token
# price step ($0.50 / $2.50 beyond it).
DEFAULT_CLASSIFIER_MODEL = ModelSpec(
    model="claude-haiku-5-5",
    price_in_usd_per_mtok=0.10,
    price_out_usd_per_mtok=0.50,
    max_output_tokens=1_024,
)


class RunConfig(Settings):
    run_id: str = Field(pattern=RUN_ID_PATTERN.pattern)
    budget: Budget = Budget()
    host_interval_s: float = Field(default=5.0, gt=0, json_schema_extra={"unit": "s"})
    concurrency: int = Field(default=8, ge=1)
    primary_provider: Provider = Provider.TAVILY
    agent_model: ModelSpec = DEFAULT_AGENT_MODEL
    classifier_model: ModelSpec = DEFAULT_CLASSIFIER_MODEL
    classifier_max_cost_usd: float = Field(
        default=0.02, gt=0, json_schema_extra={"unit": "USD"}
    )
    refresh: bool = False
    exams_first: tuple[str, ...] = ()
    gaps: tuple[str, ...] = ()
    approved_by: str | None = None
    approved_at: datetime | None = None


def new_run_id(now: datetime | None = None, token: str | None = None) -> str:
    """``cr_<UTC yyyymmddThhmmss>_<4 hex>``."""
    moment = (now or datetime.now(UTC)).astimezone(UTC)
    return f"cr_{moment:%Y%m%dT%H%M%S}_{token or secrets.token_hex(2)}"


def load_overrides(path: Path) -> dict[str, object]:
    """Read a YAML config file: a mapping of ``RunConfig`` fields to override."""
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path}: a run config is a mapping of settings")
    return {str(k): v for k, v in data.items()}


_GAP_LINE = re.compile(r"^- \[ \] \*\*(?P<text>[^*]+)\*\*")


def catalog_gaps(todo: Path) -> tuple[str, ...]:
    """The open "Exam catalog gaps" items in ``TODO.md`` (searched first, FR-005)."""
    if not todo.exists():
        return ()
    gaps: list[str] = []
    in_section = False
    for line in todo.read_text(encoding="utf-8").splitlines():
        if line.startswith("## "):
            in_section = line.strip() == "## Exam catalog gaps (spec 001)"
            continue
        match = _GAP_LINE.match(line) if in_section else None
        if match:
            gaps.append(match["text"].strip())
    return tuple(gaps)
