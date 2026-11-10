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


# --------------------------------------------------------------------------
# academic email addresses
# --------------------------------------------------------------------------

from functools import lru_cache  # noqa: E402
from pathlib import Path  # noqa: E402

_DOMAINS_FILE = Path(__file__).resolve().parent / "data" / "academic_domains.txt"
#: Second-level labels that mark an academic domain under a country code: ox.ac.uk, unimelb.edu.au.
_ACADEMIC_SLD = {"ac", "edu"}
_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")


@lru_cache(maxsize=1)
def academic_domains() -> frozenset[str]:
    lines = _DOMAINS_FILE.read_text().splitlines() if _DOMAINS_FILE.exists() else []
    return frozenset(line.strip().lower() for line in lines if line.strip() and not line.startswith("#"))


def email_domain(email: str) -> str:
    return email.rsplit("@", 1)[-1].strip().lower().rstrip(".")


def academic_domain(email: str) -> str | None:
    """The institution's registrable domain if this is an academic address, else None.

    `alice@cs.uva.nl` -> `uva.nl`; `bob@ox.ac.uk` -> `ox.ac.uk`; `carol@cs.mit.edu` -> `mit.edu`;
    `dan@uni-mannheim.de` -> `uni-mannheim.de`. A lookalike such as `uva.nl.evil.com` does not
    match, because matching is by whole labels from the right.
    """
    domain = email_domain(email)
    labels = domain.split(".")
    if len(labels) < 2 or not all(_LABEL.match(label) for label in labels):
        return None
    known = academic_domains()
    for i in range(len(labels) - 1):
        candidate = ".".join(labels[i:])
        if candidate in known:
            return candidate
    tld = labels[-1]
    if tld == "edu" and len(labels) >= 2:
        return ".".join(labels[-2:])
    if len(labels) >= 3 and labels[-2] in _ACADEMIC_SLD and len(tld) == 2:
        return ".".join(labels[-3:])
    if tld == "de" and re.match(r"^(uni|tu|th|hs|fh)-[a-z0-9-]+$", labels[-2]):
        return ".".join(labels[-2:])
    return None
