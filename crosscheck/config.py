"""Runtime configuration, read from the environment (and .env if present)."""

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
class Settings:
    provider: str = _env("CROSSCHECK_PROVIDER", "openai")
    base_url: str = _env("CROSSCHECK_BASE_URL", "https://api.groq.com/openai/v1")
    api_key: str = _env("CROSSCHECK_API_KEY")
    extract_model: str = _env("CROSSCHECK_EXTRACT_MODEL", "llama-3.3-70b-versatile")
    reason_model: str = _env("CROSSCHECK_REASON_MODEL", "llama-3.3-70b-versatile")
    concurrency: int = int(_env("CROSSCHECK_CONCURRENCY", "4"))
    db_path: Path = REPO_ROOT / _env("CROSSCHECK_DB", "data/crosscheck.db")
    data_dir: Path = REPO_ROOT / _env("CROSSCHECK_DATA_DIR", "data")

    @property
    def has_llm(self) -> bool:
        return bool(self.api_key)

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        (self.data_dir / "uploads").mkdir(exist_ok=True)
        (self.data_dir / "pages").mkdir(exist_ok=True)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)


settings = Settings()

# Bumping either of these invalidates the content-addressed extraction cache,
# so a prompt change forces re-extraction while unchanged blocks stay cached.
PROMPT_VERSION = "1"
EXTRACTOR_VERSION = "1"
