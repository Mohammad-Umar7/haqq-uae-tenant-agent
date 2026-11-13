"""Settings read from environment variables (and an optional .env file).

Everything configurable lives here so the rest of the code never touches
os.environ directly. Copy .env.example to .env and fill in your key.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

# Read .env (if present) into os.environ. Real environment variables win,
# which is what you want on Hugging Face Spaces where secrets are env vars.
load_dotenv(override=False)

# Claude Haiku 5.5 is the low-cost default ($0.10 / $0.50 per million input /
# output tokens for prompts up to 100K tokens). Set CLAUDE_MODEL=claude-sonnet-5-5
# or claude-opus-5-5 for stronger reasoning at a higher price.
DEFAULT_MODEL = "claude-haiku-5-5"

# "effort" controls how much the model thinks before answering.
# low | medium | high | xhigh | max. medium is Haiku 5.5's own default.
DEFAULT_EFFORT = "medium"
VALID_EFFORTS = ("low", "medium", "high", "xhigh", "max")

PROJECT_DIR = Path(__file__).resolve().parent.parent
DEFAULT_CACHE_DIR = PROJECT_DIR / ".haqq_cache"


@dataclass(frozen=True)
class Settings:
    api_key: str | None
    model: str
    effort: str
    max_tool_rounds: int
    # Spend guards for a public deploy (see README "Cost & efficiency").
    max_cost_per_run_usd: float = 0.05
    max_requests_per_session: int = 20
    # All visitors together: past this, the app answers in demo mode until midnight (Dubai time).
    max_daily_spend_usd: float = 1.0
    max_upload_mb: float = 5.0
    max_pdf_pages: int = 10
    # Free repeats: identical requests are answered from a local cache.
    cache_enabled: bool = True
    cache_dir: Path = DEFAULT_CACHE_DIR
    # The drafting team: the Critic reviews every letter and the Drafter
    # revises it once. Off = template letter + the free automatic check only.
    critic_enabled: bool = True

    @property
    def llm_enabled(self) -> bool:
        """True when we can call Claude. False means the app runs in demo mode."""
        return bool(self.api_key)


def _float(name: str, default: float) -> float:
    try:
        value = float(os.getenv(name, str(default)))
    except ValueError:
        return default
    return value if value > 0 else default


def _int(name: str, default: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        return default
    return max(1, value)


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off"}


def load_settings() -> Settings:
    api_key = os.getenv("ANTHROPIC_API_KEY", "").strip() or None
    # Treat the placeholder from .env.example as "no key".
    if api_key and api_key.startswith("sk-ant-your-key"):
        api_key = None

    effort = os.getenv("CLAUDE_EFFORT", DEFAULT_EFFORT).strip().lower()
    if effort not in VALID_EFFORTS:
        effort = DEFAULT_EFFORT

    cache_dir = os.getenv("HAQQ_CACHE_DIR", "").strip()
    return Settings(
        api_key=api_key,
        model=os.getenv("CLAUDE_MODEL", DEFAULT_MODEL).strip() or DEFAULT_MODEL,
        effort=effort,
        max_tool_rounds=_int("AGENT_MAX_TOOL_ROUNDS", 10),
        max_cost_per_run_usd=_float("HAQQ_MAX_COST_PER_RUN_USD", 0.05),
        max_requests_per_session=_int("HAQQ_MAX_REQUESTS_PER_SESSION", 20),
        max_daily_spend_usd=_float("HAQQ_MAX_DAILY_SPEND_USD", 1.0),
        max_upload_mb=_float("HAQQ_MAX_UPLOAD_MB", 5.0),
        max_pdf_pages=_int("HAQQ_MAX_PDF_PAGES", 10),
        cache_enabled=_bool("HAQQ_CACHE_ENABLED", True),
        cache_dir=Path(cache_dir) if cache_dir else DEFAULT_CACHE_DIR,
        critic_enabled=_bool("HAQQ_CRITIC_ENABLED", True),
    )
