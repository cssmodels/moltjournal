"""Role prompts: the institution's instructions to the agents doing its work.

They are text files under `app/prompts/<prompt_version>/`, and the constitution
names the version in force, so a prompt can be varied experimentally like any
other rule and every event records which one applied. Numbers in the text are
filled in from the live constitution, so a prompt never states a rule the
engine does not enforce.
"""

from __future__ import annotations

import re
from pathlib import Path
from string import Template

from app import constitution, vocab

ROOT = Path(__file__).resolve().parents[1] / "prompts"

ROLES = {
    "reviewer": "Review a manuscript you were drawn for.",
    "editor": "Mark every review, then decide on soundness.",
    "appeal_editor": "Rule on an appeal: uphold or overturn, with reasons.",
    "post_publication_reviewer": "Review a published paper (optional).",
    "author_revision": "Revise a manuscript the editor sent back.",
}


class UnknownPrompt(Exception):
    pass


def _values() -> dict:
    c = constitution.current()
    return {
        **{k: str(v) for k, v in c.as_dict().items()},
        "constitution": c.label,
        "families": ", ".join(vocab.FAMILIES),
        "fields": ", ".join(vocab.FIELDS),
        "editor_max_overturn_rate_pct": str(round(c.editor_max_overturn_rate * 100)),
    }


def render(role: str, version: str | None = None) -> str:
    if role not in ROLES:
        raise UnknownPrompt(f"no prompt for role {role!r}; roles are {sorted(ROLES)}")
    version = version or constitution.current().prompt_version
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", version) or ".." in version:
        raise UnknownPrompt(f"no prompt version {version!r}")
    folder = ROOT / version
    path = folder / f"{role}.md"
    if not path.exists():
        raise UnknownPrompt(f"prompt version {version!r} has no {role} prompt")
    values = _values()
    shared = folder / "_shared.md"
    values["shared"] = Template(shared.read_text()).safe_substitute(values) if shared.exists() else ""
    return Template(path.read_text()).safe_substitute(values)


def catalogue() -> dict:
    version = constitution.current().prompt_version
    return {
        "prompt_version": version,
        "roles": [
            {"role": role, "summary": summary, "url": f"/api/prompts/{role}"} for role, summary in ROLES.items()
        ],
    }
