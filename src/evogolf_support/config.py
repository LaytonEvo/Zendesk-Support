"""Configuration loaded from the environment (see .env.example)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def load_dotenv(path: Path | None = None) -> None:
    """Load KEY=VALUE pairs from .env into os.environ without overwriting.

    Deliberately minimal so the project has no dependency on python-dotenv.
    """
    env_path = path or REPO_ROOT / ".env"
    if not env_path.is_file():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


class ConfigError(RuntimeError):
    """A required setting is missing."""


def _require(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ConfigError(
            f"{name} is not set. On Railway, add it in the service's Variables "
            f"tab; locally, copy .env.example to .env and fill it in."
        )
    return value


@dataclass(frozen=True)
class ZendeskConfig:
    subdomain: str
    email: str
    api_token: str

    @property
    def base_url(self) -> str:
        return f"https://{self.subdomain}.zendesk.com/api/v2"

    @property
    def auth(self) -> tuple[str, str]:
        # Zendesk API-token auth: username is "<email>/token".
        return (f"{self.email}/token", self.api_token)

    @classmethod
    def from_env(cls) -> "ZendeskConfig":
        load_dotenv()
        return cls(
            subdomain=_require("ZENDESK_SUBDOMAIN"),
            email=_require("ZENDESK_EMAIL"),
            api_token=_require("ZENDESK_API_TOKEN"),
        )


def corpus_path() -> Path:
    load_dotenv()
    raw = os.environ.get("CORPUS_DB", "data/corpus.sqlite3")
    path = Path(raw)
    if not path.is_absolute():
        path = REPO_ROOT / path
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def redact_pii() -> bool:
    load_dotenv()
    return os.environ.get("REDACT_PII", "true").strip().lower() in {"1", "true", "yes"}
