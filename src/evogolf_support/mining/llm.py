"""Anthropic client setup for the mining jobs."""

from __future__ import annotations

import os

import anthropic

from ..config import ConfigError, load_dotenv

# Opus is the right call here: this runs a few dozen times total, against the
# corpus that every later draft is built on. Getting the policy wrong is far
# more expensive than the tokens.
MODEL = "claude-opus-5"


def client() -> anthropic.Anthropic:
    load_dotenv()
    if not os.environ.get("ANTHROPIC_API_KEY", "").strip():
        raise ConfigError(
            "ANTHROPIC_API_KEY is not set. On Railway, add it in the service's "
            "Variables tab; locally, put it in .env."
        )
    return anthropic.Anthropic()
