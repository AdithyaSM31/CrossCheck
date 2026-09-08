"""Runtime configuration, read from the environment (and .env if present).

Extraction and reasoning are configured **independently**. They are different jobs: one is
thousands of high-volume, low-judgement calls where a free tier is ideal and the grounding
gate catches what a weaker model gets wrong; the other is a few hundred calls where the
model's judgement decides the output. Splitting them means the expensive model is only
spent where it changes the answer.

Each role falls back to the generic CROSSCHECK_* settings when its own are unset, so a
single-provider setup stays a three-line .env.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

REPO_ROOT = Path(__file__).resolve().parent.parent


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


@dataclass(frozen=True)
class RoleConfig:
    """How one role (extraction or reasoning) talks to a model."""

    role: str
    provider: str  # "openai" (any OpenAI-compatible endpoint) | "anthropic"
    base_url: str
    api_key: str
    model: str

    @property
    def configured(self) -> bool:
        return bool(self.api_key and self.model)

    def describe(self) -> str:
        host = self.base_url.split("//")[-1].split("/")[0] or "?"
        return f"{self.role}: {self.model} via {host}"


def _role(name: str, default_model: str) -> RoleConfig:
    up = name.upper()
    return RoleConfig(
        role=name,
        provider=_env(f"CROSSCHECK_{up}_PROVIDER") or _env("CROSSCHECK_PROVIDER", "openai"),
        base_url=(
            _env(f"CROSSCHECK_{up}_BASE_URL")
            or _env("CROSSCHECK_BASE_URL", "https://api.groq.com/openai/v1")
        ),
        api_key=_env(f"CROSSCHECK_{up}_API_KEY") or _env("CROSSCHECK_API_KEY"),
        model=(
            _env(f"CROSSCHECK_{up}_MODEL")
            or _env("CROSSCHECK_MODEL")
            or default_model
        ),
    )


@dataclass(frozen=True)
class Settings:
    extract: RoleConfig = _role("extract", "llama-3.3-70b-versatile")
    reason: RoleConfig = _role("reason", "gpt-4.1-mini")

    concurrency: int = int(_env("CROSSCHECK_CONCURRENCY", "4") or 4)
    # A hard ceiling on model calls per command. 0 disables it. This exists because a
    # runaway extraction loop against a paid endpoint is an expensive way to find a bug.
    max_calls: int = int(_env("CROSSCHECK_MAX_CALLS", "0") or 0)
    request_timeout: float = float(_env("CROSSCHECK_TIMEOUT", "120") or 120)

    db_path: Path = REPO_ROOT / _env("CROSSCHECK_DB", "data/crosscheck.db")
    data_dir: Path = REPO_ROOT / _env("CROSSCHECK_DATA_DIR", "data")

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        (self.data_dir / "uploads").mkdir(exist_ok=True)
        (self.data_dir / "pages").mkdir(exist_ok=True)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)


settings = Settings()

# Bumping either invalidates the content-addressed cache, so a prompt change forces
# re-extraction while unchanged blocks stay cached.
PROMPT_VERSION = "1"
EXTRACTOR_VERSION = "1"
