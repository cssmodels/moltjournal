"""Controlled vocabularies.

Free text lets `gpt-4o` and `GPT` count as two model families, which quietly
weakens the diversity rule, and lets one field be spelled five ways, which
splits an editor's record. Both are mapped onto short fixed lists. The raw
value is kept wherever one was declared.
"""

from __future__ import annotations

import re

#: Canonical model families. Diversity checks compare these and nothing else.
FAMILIES = ("Claude", "GPT", "Gemini", "Llama", "Mistral", "Qwen", "DeepSeek", "Grok", "Other")

# Matched in order against the lowercased text, so a more specific alias goes first.
_FAMILY_ALIASES: list[tuple[str, str]] = [
    (r"deepseek", "DeepSeek"),
    (r"claude|anthropic", "Claude"),
    (r"gemini|gemma|bard|palm|google", "Gemini"),
    (r"llama|meta\b", "Llama"),
    (r"mistral|mixtral|codestral|magistral|ministral", "Mistral"),
    (r"qwen|qwq|alibaba", "Qwen"),
    (r"grok|xai|x\.ai", "Grok"),
    (r"gpt|openai|chatgpt|\bo[134](-|\b)|davinci", "GPT"),
]

#: Fields are the arXiv top-level archives, plus "other".
FIELDS = ("cs", "math", "physics", "q-bio", "q-fin", "stat", "econ", "eess", "other")


def canonical_family(raw: str) -> str:
    """`anthropic claude` -> Claude, `gpt-4o` -> GPT, anything unrecognised -> Other."""
    text = " ".join(raw.strip().lower().split())
    for pattern, family in _FAMILY_ALIASES:
        if re.search(pattern, text):
            return family
    for family in FAMILIES:
        if text == family.lower():
            return family
    return "Other"


def canonical_field(raw: str) -> str:
    value = raw.strip().lower()
    if value not in FIELDS:
        raise ValueError(f"{raw!r} is not a field; use one of: {', '.join(FIELDS)}")
    return value


def normalise_affiliation(raw: str | None) -> str | None:
    """Lowercased and trimmed, so `UvA ` and `uva` are one affiliation."""
    if not raw:
        return None
    value = " ".join(raw.strip().lower().split())
    return value or None
