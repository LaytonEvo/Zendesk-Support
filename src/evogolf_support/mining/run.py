"""Run the mining pipeline and store the result."""

from __future__ import annotations

import json
import logging

from ..corpus.store import CorpusStore
from .discover import Taxonomy
from .mine import ThemeGuide, mine_theme
from .reclassify import reclassify

log = logging.getLogger(__name__)

GUIDE_KEY = "voice_guide"

# Below this, a theme cannot support a rule worth writing down - saying so is
# more useful than inventing a policy from four replies.
MIN_AGENT_REPLIES = 30

# Themes that are not customer support, whatever their volume.
NON_SUPPORT_THEMES = {
    "b2b_supplier_marketing_pitches",
    "internal_test_noise",
}


def themes_worth_mining(store: CorpusStore, taxonomy: Taxonomy) -> list[tuple[str, str, str]]:
    evidence = store.theme_agent_replies()
    chosen = []
    for theme in taxonomy.themes:
        if theme.key in NON_SUPPORT_THEMES:
            continue
        if evidence.get(theme.key, 0) < MIN_AGENT_REPLIES:
            log.info(
                "Skipping %s: %s agent replies, below the %s needed",
                theme.key, evidence.get(theme.key, 0), MIN_AGENT_REPLIES,
            )
            continue
        chosen.append((theme.key, theme.label, theme.definition))
    return chosen


def run_mining(store: CorpusStore, taxonomy: Taxonomy) -> dict[str, ThemeGuide]:
    """Re-classify ambiguous tickets, then mine each theme with real evidence."""
    reclassify(store, taxonomy)
    log.info("counts after re-classification: %s", store.theme_counts())
    log.info("agent replies after re-classification: %s", store.theme_agent_replies())

    guides: dict[str, ThemeGuide] = {}
    for key, label, definition in themes_worth_mining(store, taxonomy):
        guide = mine_theme(store, key, label, definition)
        if guide is None:
            continue
        guides[key] = guide
        # Logged per theme so the result is readable without exposing an endpoint.
        log.info("guide/%s: %s", key, guide.model_dump_json())

    store.set_state(
        GUIDE_KEY,
        json.dumps({k: v.model_dump() for k, v in guides.items()}),
    )
    log.info("Stored a guide covering %s themes", len(guides))
    return guides
