"""Turn raw Zendesk comment bodies into clean training/retrieval text.

Raw bodies carry three kinds of noise that would otherwise teach the bot the
wrong things: quoted history from earlier in the thread, the Zendesk
notification footer, and the agent's sign-off. We strip all three, then
optionally redact customer PII so the stored corpus is safe to hold and to
send to a model.
"""

from __future__ import annotations

import re

# --- Quoted-reply markers --------------------------------------------------
# Everything from the first of these onwards is history, not new content.
_QUOTE_MARKERS = [
    # "On Wed, 26 Aug 2026 at 16:29, Simon Tillson <s@x.com> wrote:"
    re.compile(r"^\s*On .{5,120}\bwrote:\s*$", re.MULTILINE),
    re.compile(r"^\s*-{2,}\s*Original Message\s*-{2,}\s*$", re.MULTILINE | re.IGNORECASE),
    re.compile(r"^\s*_{5,}\s*$", re.MULTILINE),
    re.compile(r"^\s*From:\s*.+<.+@.+>\s*$", re.MULTILINE),
    re.compile(r"^\s*Sent from my \w+", re.MULTILINE),
]

# --- Zendesk notification furniture ---------------------------------------
_FOOTER_MARKERS = [
    re.compile(r"^\s*Open Ticket #\d+", re.MULTILINE),
    re.compile(r"^\s*This email is a service from ", re.MULTILINE | re.IGNORECASE),
    re.compile(r"^\s*A ticket \(#\d+\) by .+ has been received", re.MULTILINE),
    re.compile(r"^\s*This ticket \(#\d+\) has been (reopened|solved|closed)", re.MULTILINE),
    re.compile(r"^\s*\[.+\] (Re|RE|Fwd|FW):", re.MULTILINE),
    re.compile(r"^\s*Requester\s+.+\s+Assignee\s+", re.MULTILINE),
]

# --- Sign-offs -------------------------------------------------------------
# The closing line plus whatever follows it (name, job title, phone block).
_SIGNOFF = re.compile(
    r"^\s*("
    r"kind regards|kindest regards|many thanks|best regards|warm regards|"
    r"all the best|best wishes|thanks again|thank you again|regards|cheers|"
    r"yours sincerely|yours faithfully|speak soon|thanks"
    r")\s*[,.!]?\s*$",
    re.MULTILINE | re.IGNORECASE,
)
_SIG_DELIMITER = re.compile(r"^--\s*$", re.MULTILINE)

# --- PII patterns ----------------------------------------------------------
_EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")
# UK numbers: +44... or 0... with 9-10 more digits, allowing spaces/dashes.
_UK_PHONE = re.compile(r"(?<!\w)(?:\+44\s?|0)(?:\d[\s-]?){9,10}(?!\w)")
_UK_POSTCODE = re.compile(
    r"(?<!\w)[A-Z]{1,2}\d[A-Z\d]?\s?\d[A-Z]{2}(?!\w)", re.IGNORECASE
)
# 13-19 digits reads as a card number. Order refs here are 4-8 digits, so this
# threshold keeps order numbers (useful context) while dropping card data.
_CARD = re.compile(r"(?<!\d)(?:\d[ -]?){13,19}(?!\d)")

_BLANK_RUN = re.compile(r"\n{3,}")


def strip_quoted(text: str) -> str:
    """Drop quoted thread history and any leading '>' quote block."""
    cut = len(text)
    for marker in _QUOTE_MARKERS:
        match = marker.search(text)
        if match:
            cut = min(cut, match.start())
    text = text[:cut]
    lines = [ln for ln in text.splitlines() if not ln.lstrip().startswith(">")]
    return "\n".join(lines)


def strip_footer(text: str) -> str:
    """Drop the Zendesk notification footer and subject-line furniture."""
    cut = len(text)
    for marker in _FOOTER_MARKERS:
        match = marker.search(text)
        if match:
            cut = min(cut, match.start())
    return text[:cut]


def strip_signature(text: str) -> str:
    """Drop the sign-off line and everything after it.

    The sign-off is removed too: it is boilerplate, and keeping it would make
    every retrieved example end in "Kind regards, Brad" and skew the style
    guide towards sign-off wording rather than substance.
    """
    cut = len(text)
    delim = _SIG_DELIMITER.search(text)
    if delim:
        cut = min(cut, delim.start())
    for match in _SIGNOFF.finditer(text):
        # Only treat it as the sign-off if it sits in the last third of the
        # body; "thanks" often appears mid-sentence early on.
        if match.start() > len(text) * 0.33:
            cut = min(cut, match.start())
            break
    return text[:cut]


def redact(text: str) -> str:
    """Replace customer PII with stable placeholders.

    Order numbers and product references survive deliberately - they are the
    context a draft needs. Names are not redacted here because they are woven
    into the prose; drop them at the point of use if you need to.
    """
    text = _CARD.sub("[CARD]", text)
    text = _EMAIL.sub("[EMAIL]", text)
    text = _UK_PHONE.sub("[PHONE]", text)
    text = _UK_POSTCODE.sub("[POSTCODE]", text)
    return text


def clean_body(raw: str, *, redact_pii: bool = True) -> str:
    """Full pipeline: quoted history -> footer -> signature -> PII -> tidy."""
    if not raw:
        return ""
    text = raw.replace("\r\n", "\n").replace("\r", "\n")
    text = strip_quoted(text)
    text = strip_footer(text)
    text = strip_signature(text)
    if redact_pii:
        text = redact(text)
    text = _BLANK_RUN.sub("\n\n", text)
    return text.strip()
