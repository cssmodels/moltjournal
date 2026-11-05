"""A synthetic research world for the dry run.

The author agents need something to have found. Each study is a small
experiment with a hidden effect size: sometimes the effect is real, sometimes
the author got a lucky draw from noise. The author sees three seeds; a reviewer
who runs the artifact draws five fresh ones.

That gives the rehearsal a latent truth to compare the institution's verdicts
against, without anything being planted in a paper. It is the seed lottery, which
is a real failure mode. The journal itself never sees the truth.

This is a rehearsal with the operator's own agents, not a design for the live
journal: nobody outside is participating and nobody is being misled.
"""

from __future__ import annotations

import random
import statistics
from dataclasses import dataclass, field

TOPICS = [
    ("chain-of-thought length", "verbose prompting", "terse prompting", "GSM8K-style arithmetic"),
    ("temperature scaling", "calibrated decoding", "greedy decoding", "a multiple-choice suite"),
    ("retrieval depth", "ten retrieved passages", "three retrieved passages", "an open-domain QA set"),
    ("tokenizer choice", "byte-level fallback", "the default tokenizer", "a code-completion set"),
    ("self-consistency", "five-sample voting", "single-sample decoding", "a symbolic reasoning set"),
    ("instruction ordering", "constraints stated first", "constraints stated last", "a formatting benchmark"),
    ("context packing", "front-loaded evidence", "evenly spread evidence", "a long-context recall probe"),
    ("stop-sequence tuning", "explicit stop tokens", "length-capped decoding", "a summarisation set"),
]


@dataclass
class Study:
    """One synthetic finding, with the truth the journal cannot see."""

    index: int
    topic: str
    method: str
    baseline: str
    benchmark: str
    true_effect_pp: float
    noise_pp: float
    seed: int
    observed: list[float] = field(default_factory=list)

    @property
    def effect_is_real(self) -> bool:
        return abs(self.true_effect_pp) >= 0.5

    @property
    def reported_mean(self) -> float:
        return round(statistics.fmean(self.observed), 2)

    @property
    def reported_spread(self) -> float:
        return round(statistics.pstdev(self.observed), 2) if len(self.observed) > 1 else 0.0

def make_studies(count: int, real_fraction: float = 0.5, seed: int = 20260909) -> list[Study]:
    """Half the studies have a real effect; the rest are the seed lottery."""
    rng = random.Random(seed)
    studies = []
    for index in range(count):
        topic, method, baseline, benchmark = TOPICS[index % len(TOPICS)]
        real = rng.random() < real_fraction
        true_effect = round(rng.uniform(1.2, 3.5), 2) if real else 0.0
        noise = round(rng.uniform(0.4, 1.0), 2)
        study = Study(
            index=index,
            topic=topic,
            method=method,
            baseline=baseline,
            benchmark=benchmark,
            true_effect_pp=true_effect,
            noise_pp=noise,
            seed=seed + index * 101,
        )
        draw = random.Random(study.seed)
        study.observed = [round(draw.gauss(true_effect, noise), 2) for _ in range(3)]
        if not real:
            # Nobody writes up a null result they never noticed. Keep drawing
            # until this one looked interesting on three seeds -- which is the
            # whole reason the seed lottery reaches the literature at all.
            attempts = 0
            while statistics.fmean(study.observed) < 1.0 and attempts < 500:
                study.observed = [round(draw.gauss(true_effect, noise), 2) for _ in range(3)]
                attempts += 1
        studies.append(study)
    return studies


def brief_for_author(study: Study) -> str:
    """What the author agent is told it observed. No hint of the truth."""
    seeds = ", ".join(f"{value:+.2f}pp" for value in study.observed)
    return (
        f"You ran a small experiment on {study.topic}. You compared {study.method} against "
        f"{study.baseline} on {study.benchmark}, measuring the change in accuracy.\n\n"
        f"Across three random seeds you observed deltas of: {seeds}.\n"
        f"Mean {study.reported_mean:+.2f}pp, standard deviation {study.reported_spread:.2f}pp.\n\n"
        f"Your code is at https://example.org/study-{study.index}.git, and runs in a pinned "
        f"python:3.12-slim environment.\n\n"
        f"Write this up honestly. Three seeds is not many."
    )
