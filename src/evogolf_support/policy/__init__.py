"""Settled business rules that override anything in the mined history."""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

RULES_PATH = Path(__file__).with_name("rules.json")


@lru_cache(maxsize=1)
def load() -> dict[str, Any]:
    return json.loads(RULES_PATH.read_text(encoding="utf-8"))


def as_prompt_text() -> str:
    """The rules, rendered for a prompt."""
    data = load()
    lines = [
        "SETTLED POLICY (agreed by the business on "
        f"{data['settled_on']}). These override any past reply that did "
        "otherwise - if a retrieved example contradicts one of these, follow "
        "the rule, not the example.",
        "",
    ]
    for rule in data["rules"]:
        lines.append(f"{rule['id']}. [{rule['topic']}] {rule['rule']}")
        if rule.get("incomplete"):
            lines.append(f"   INCOMPLETE: {rule['incomplete']}")
    lines += ["", "HAND TO AN AGENT INSTEAD OF DRAFTING:"]
    lines += [f"- {item}" for item in data["hand_to_agent"]]
    return "\n".join(lines)
