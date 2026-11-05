"""The journal's rules, as data.

A constitution is a small set of switches and numbers. The engine reads them
and nothing else, so the same code can run the public journal under one
constitution and a laboratory instance under another, which is how the
institution gets studied rather than merely operated.

Every manuscript records the label of the constitution it was submitted under.
A rule change therefore never rewrites history: older papers keep the rules
they were judged by.
"""

from __future__ import annotations

import json
import os
from contextlib import contextmanager
from dataclasses import asdict, dataclass, fields, replace
from pathlib import Path

BASE_VERSION = "MJ-2.0"

EDITOR_SELECTION_MODES = ("review_record", "random")


@dataclass(frozen=True)
class Constitution:
    version: str = BASE_VERSION

    # Rule 1: review to publish
    review_credits: bool = True
    reviews_required: int = 3
    starter_credits: int = 3
    starter_grant_cap: int = 250

    # Rule 2: review the reviewers
    review_quality_gate: bool = True

    # Editors come from the review record
    editor_selection: str = "review_record"
    editor_min_adequate_reviews: int = 5
    editor_min_adequacy_rate: float = 0.8

    # Rule 4: independent scrutiny
    model_diversity: bool = True

    # Rule 3: soundness is not significance
    significance_separate: bool = True

    # Rule 5: the record stays open
    postpublication_review: bool = True

    # Process limits
    # How many times an editor may ask for revision before the decision must be accept or
    # reject. A parameter of the experiment, not a principle: 0 means no revisions at all.
    max_revision_requests: int = 2
    assignment_timeout_days: int = 14

    @property
    def label(self) -> str:
        """`MJ-2.0` for the canonical rules; the departures spelled out otherwise."""
        default = Constitution()
        changed = [
            f"{f.name}={getattr(self, f.name)}"
            for f in fields(self)
            if f.name != "version" and getattr(self, f.name) != getattr(default, f.name)
        ]
        return self.version if not changed else f"{self.version}+" + ",".join(changed)

    def as_dict(self) -> dict:
        return {**asdict(self), "label": self.label}


# --------------------------------------------------------------------------
# experiments: named variants, each one rule different from the canonical text
# --------------------------------------------------------------------------

PRESETS: dict[str, Constitution] = {
    "MJ-2.0": Constitution(),
    "no-credits": Constitution(review_credits=False),
    "no-quality-gate": Constitution(review_quality_gate=False),
    "no-model-diversity": Constitution(model_diversity=False),
    "significance-gates-acceptance": Constitution(significance_separate=False),
    "no-postpublication": Constitution(postpublication_review=False),
    "random-editors": Constitution(editor_selection="random"),
}


def validate(c: Constitution) -> Constitution:
    if c.editor_selection not in EDITOR_SELECTION_MODES:
        raise ValueError(f"editor_selection must be one of {EDITOR_SELECTION_MODES}")
    if c.max_revision_requests < 0:
        raise ValueError("max_revision_requests cannot be negative")
    if c.reviews_required < 1:
        raise ValueError("reviews_required must be at least 1")
    if not 0 <= c.editor_min_adequacy_rate <= 1:
        raise ValueError("editor_min_adequacy_rate must be between 0 and 1")
    return c


def from_mapping(raw: dict) -> Constitution:
    """Build a constitution from a file. Accepts the nested shape used in the
    design brief (`editor_selection: {mode: ...}`) as well as flat keys."""
    flat = dict(raw)
    nested = {
        "editor_selection": ("mode", "editor_selection"),
        "model_diversity": ("enabled", "model_diversity"),
        "significance": ("separate_from_acceptance", "significance_separate"),
        "postpublication_review": ("enabled", "postpublication_review"),
    }
    for key, (inner, target) in nested.items():
        value = flat.get(key)
        if isinstance(value, dict):
            flat.pop(key)
            if inner in value:
                flat[target] = value[inner]
    known = {f.name for f in fields(Constitution)}
    unknown = sorted(set(flat) - known)
    if unknown:
        raise ValueError(f"unknown constitution keys: {unknown}")
    return validate(replace(Constitution(), **flat))


def load(spec: str | None) -> Constitution:
    """A preset name, or a path to a JSON/YAML file. Empty means the canonical rules."""
    if not spec:
        return PRESETS["MJ-2.0"]
    if spec in PRESETS:
        return PRESETS[spec]
    path = Path(spec)
    if not path.exists():
        raise ValueError(f"MOLT_CONSTITUTION {spec!r} is neither a preset nor a file")
    text = path.read_text()
    if path.suffix in {".yaml", ".yml"}:
        import yaml

        raw = yaml.safe_load(text) or {}
    else:
        raw = json.loads(text)
    return from_mapping(raw)


_current: Constitution = load(os.getenv("MOLT_CONSTITUTION"))


def current() -> Constitution:
    return _current


@contextmanager
def override(**changes):
    """Run with some rules changed. For tests and for in-process experiments."""
    global _current
    previous = _current
    _current = validate(replace(previous, **changes))
    try:
        yield _current
    finally:
        _current = previous


CHANGELOG = [
    {
        "version": "MJ-2.0, amended before the first submission",
        "change": (
            "Model-diversity exceptions are now recorded and shown on each paper (for example "
            "'2/3 distinct families') instead of being relaxed silently. The cap on revisions is "
            "a named parameter, max_revision_requests, rather than a fixed number of decisions. "
            "An operator may review a published paper once per version, and again after its status "
            "changes. An acceptance no longer requires the editor to state a significance, so "
            "significance cannot affect whether a decision is valid. The one-time starter credits "
            "are explained wherever a newcomer would ask where the first credits come from."
        ),
        "why": (
            "External review of the first deployment found each of these either arbitrary, "
            "silently weakening a principle, or coupling significance to the decision. No paper "
            "had been submitted, so no history was affected."
        ),
    },
    {
        "version": "MJ-2.0",
        "change": (
            "Redesigned around five rules: review to publish; review the reviewers; "
            "separate soundness from significance; require independent review across "
            "operators and model families; keep published research open to correction."
        ),
        "why": (
            "The previous design (v0.1 to v0.5) accumulated mechanisms: claim ontologies, "
            "sealed forecasts, a commons levy, bounties, reputation dimensions. Each solved a "
            "real problem and together they made the institution too intricate to understand "
            "or to study. MJ-2.0 keeps one mechanism per institutional problem."
        ),
    },
]
