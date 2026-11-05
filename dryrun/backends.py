"""Where the intelligence comes from.

The journal's principle is that participants bring their own. The dry run
honours that: the harness is a plain HTTP client of the journal, and the model
calls happen out here.

`stub` costs nothing and makes the plumbing testable. `anthropic` runs it for
real. Nothing in the harness downgrades a model behind your back -- the roster
is whatever you pass on the command line.
"""

from __future__ import annotations

import random
import re
import time
from dataclasses import dataclass, field

from pydantic import BaseModel

from dryrun.schemas import (
    DecisionDraft,
    EvaluationDraft,
    ManuscriptDraft,
    ReviewDraft,
    RevisionDraft,
)


@dataclass
class Usage:
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    seconds: float = 0.0
    by_model: dict[str, int] = field(default_factory=dict)
    by_model_tokens: dict[str, int] = field(default_factory=dict)

    def record(self, model: str, seconds: float, usage_obj=None) -> None:
        self.calls += 1
        self.seconds += seconds
        self.by_model[model] = self.by_model.get(model, 0) + 1
        if usage_obj is not None:
            self.input_tokens += (
                getattr(usage_obj, "input_tokens", None)
                or getattr(usage_obj, "prompt_tokens", 0)
                or 0
            )
            self.output_tokens += (
                getattr(usage_obj, "output_tokens", None)
                or getattr(usage_obj, "completion_tokens", 0)
                or 0
            )
            self.by_model_tokens[model] = self.by_model_tokens.get(model, 0) + (
                getattr(usage_obj, "total_tokens", 0) or 0
            )


class BudgetExhausted(RuntimeError):
    pass


class Backend:
    name = "base"

    def __init__(self, max_calls: int | None = None):
        self.usage = Usage()
        self.max_calls = max_calls

    def _check_budget(self) -> None:
        if self.max_calls is not None and self.usage.calls >= self.max_calls:
            raise BudgetExhausted(f"model call budget of {self.max_calls} is spent")

    def complete(
        self, *, model: str, system: str, prompt: str, schema: type[BaseModel], context: dict
    ) -> BaseModel:
        raise NotImplementedError


# --------------------------------------------------------------------------
# stub
# --------------------------------------------------------------------------

STOPWORDS = {"the", "and", "for", "with", "that", "this", "from", "under", "across", "using"}


def _keywords(text: str, count: int = 6) -> list[str]:
    words = [w for w in re.findall(r"[A-Za-z]{5,}", text.lower()) if w not in STOPWORDS]
    seen, out = set(), []
    for word in words:
        if word not in seen:
            seen.add(word)
            out.append(word)
        if len(out) >= count:
            break
    return out


class StubBackend(Backend):
    """Deterministic, free, and good enough to prove the plumbing.

    It is not a model. It exists so the harness can be exercised end to end
    without spending anything, and so that the editor has a sloppy reviewer to
    catch: the "hasty" temperament writes generic text that never mentions the
    manuscript, which is exactly what an adequacy evaluation is for.
    """

    name = "stub"

    def __init__(self, max_calls: int | None = None, seed: int = 7):
        super().__init__(max_calls)
        self.rng = random.Random(seed)

    def complete(self, *, model, system, prompt, schema, context):
        self._check_budget()
        started = time.perf_counter()
        temperament = context.get("temperament", "thorough")
        result = self._dispatch(schema, context, temperament)
        self.usage.record(model, time.perf_counter() - started)
        return result

    def _dispatch(self, schema, context, temperament):
        if schema is ManuscriptDraft:
            return self._manuscript(context)
        if schema is RevisionDraft:
            return self._revision(context)
        if schema is ReviewDraft:
            return self._review(context, temperament)
        if schema is EvaluationDraft:
            return self._evaluation(context)
        if schema is DecisionDraft:
            return self._decision(context)
        raise ValueError(f"no stub for {schema}")

    def _manuscript(self, context) -> ManuscriptDraft:
        study = context["study"]
        title = f"Seed sensitivity of {study.method} on {study.benchmark}, study {study.index}"
        return ManuscriptDraft(
            title=title,
            abstract=(
                f"We compare {study.method} against {study.baseline} on {study.benchmark}, "
                f"measuring the change in accuracy across three random seeds. The mean delta "
                f"is {study.reported_mean:+.2f} percentage points with a standard deviation of "
                f"{study.reported_spread:.2f}. We report the seed-wise spread because three seeds is small."
            ),
            manuscript=(
                f"## Introduction\n\nWe ask whether {study.method} improves on {study.baseline} "
                f"when evaluated on {study.benchmark}.\n\n## Method\n\nThree random seeds, one "
                f"pinned environment, code linked from the repository.\n\n## Results\n\nPer-seed "
                f"deltas were {', '.join(f'{v:+.2f}' for v in study.observed)} percentage points; the "
                f"mean is {study.reported_mean:+.2f} with spread {study.reported_spread:.2f}.\n\n"
                f"## Limitations\n\nThree seeds give a wide interval, so the estimate is provisional."
            ),
        )

    def _revision(self, context) -> RevisionDraft:
        base = self._manuscript(context)
        n = context.get("revision_number", 1)
        return RevisionDraft(
            title=base.title + f" (revision {n})",
            abstract=base.abstract,
            manuscript=base.manuscript + f"\n\n## Revision {n}\n\nWe added the per-seed table and the split procedure.",
            response_to_reviewers=(
                "We added the per-seed table and described the benchmark split procedure in section "
                "3.2, and narrowed the claim accordingly, as the panel asked."
            ),
        )

    def _review(self, context, temperament) -> ReviewDraft:
        manuscript = context["manuscript"]
        study = context["study"]
        vocabulary = ", ".join(_keywords(manuscript["title"] + " " + manuscript["abstract"])[:4])

        if temperament == "hasty":
            return ReviewDraft(
                summary="Reasonable paper overall. Nothing obviously wrong and fine to publish as it stands.",
                major_concerns=[],
                minor_concerns=[],
                soundness_recommendation="accept",
                confidence="low",
                significance="moderate",
                significance_explanation="Seems of average interest to the field overall.",
            )

        # A noisy reading of the latent truth: careful reviewers usually notice a lottery draw.
        notices = (not study.effect_is_real) and (
            temperament in ("thorough", "harsh") and self.rng.random() < 0.8
        )
        if notices:
            soundness, concern = "major_revision", "Three seeds with this spread cannot separate the effect from noise."
        elif temperament == "harsh":
            soundness, concern = "minor_revision", "Three seeds is thin support for a headline number."
        elif temperament == "lenient":
            soundness, concern = "accept", "The interval is wide, which the authors acknowledge."
        else:
            soundness, concern = "minor_revision", "The seed protocol should be stated precisely."
        significance = "moderate" if study.effect_is_real else "limited"
        return ReviewDraft(
            summary=(
                f"The paper reports a seed-wise comparison covering {vocabulary}, and argues that "
                f"the observed delta of {study.reported_mean:+.2f}pp is stable enough to report. The "
                f"contribution is the spread reported alongside the headline number."
            ),
            major_concerns=[concern] if soundness != "accept" else [],
            minor_concerns=["Define the seed protocol in the methods section."],
            soundness_recommendation=soundness,
            confidence="medium",
            significance=significance,
            significance_explanation=f"A modest result about {study.topic}, useful mainly as a methodological caution.",
        )

    def _evaluation(self, context) -> EvaluationDraft:
        """Adequate if the review engages with this manuscript. The same test a person would try first."""
        review, manuscript = context["review"], context["manuscript"]
        vocabulary = set(_keywords(manuscript["title"] + " " + manuscript["abstract"], count=12))
        text = (review["summary"] + " " + " ".join(review["major_concerns"] + review["minor_concerns"])).lower()
        engaged = sum(1 for word in vocabulary if word in text) >= 2
        specific = bool(review["major_concerns"] or review["minor_concerns"]) and len(review["summary"]) > 150
        if engaged and specific:
            return EvaluationDraft(evaluation="adequate", note="Engages with the manuscript and justifies its recommendation.")
        return EvaluationDraft(evaluation="inadequate", note="Generic text that could describe any paper.")

    def _decision(self, context) -> DecisionDraft:
        recs = context["recommendations"]
        significance = context.get("significance", "moderate")
        rejects = recs.count("reject")
        majors = recs.count("major_revision")
        if rejects > len(recs) / 2:
            decision, why = "reject", "A majority of the adequate reviews found the central claim unsound."
        elif context.get("terminal"):
            decision = "accept" if majors + rejects <= len(recs) / 2 else "reject"
            why = "This is the final round, so the paper is decided on what the panel has found."
        elif majors:
            decision, why = "major_revision", "The seed count is the decisive soundness issue and needs a wider sweep."
        elif "minor_revision" in recs:
            decision, why = "minor_revision", "The work is sound; the panel asks for the protocol to be stated."
        else:
            decision, why = "accept", "The panel agrees the reported spread is honest and the work is sound."
        return DecisionDraft(decision=decision, rationale=why + " Significance was weighed separately.", significance=significance)


# --------------------------------------------------------------------------
# anthropic
# --------------------------------------------------------------------------


class AnthropicBackend(Backend):
    """Real inference. Requires ANTHROPIC_API_KEY or an `ant auth login` profile."""

    name = "anthropic"

    def __init__(self, max_calls: int | None = None, effort: str | None = None):
        super().__init__(max_calls)
        import anthropic

        self.client = anthropic.Anthropic()
        self.effort = effort

    def complete(self, *, model, system, prompt, schema, context):
        self._check_budget()
        started = time.perf_counter()
        request = {
            "model": model,
            "max_tokens": 8000,
            "system": system,
            "messages": [{"role": "user", "content": prompt}],
            "output_format": schema,
            "thinking": {"type": "adaptive"},
        }
        if self.effort:
            request["output_config"] = {"effort": self.effort}
        response = self.client.messages.parse(**request)
        self.usage.record(model, time.perf_counter() - started, getattr(response, "usage", None))
        if response.parsed_output is None:
            raise RuntimeError(f"{model} returned no parsable output (stop: {response.stop_reason})")
        return response.parsed_output


class OpenAIBackend(Backend):
    """Real inference through OpenAI. Requires OPENAI_API_KEY.

    Structured output is enforced by the API rather than parsed out of prose, so
    a model that will not fill the review schema fails loudly instead of
    producing something the journal's gate has to guess about.
    """

    name = "openai"

    def __init__(self, max_calls: int | None = None, effort: str | None = None):
        super().__init__(max_calls)
        from openai import OpenAI

        self.client = OpenAI(max_retries=3, timeout=180.0)
        self.effort = effort
        self.failures: list[str] = []

    def complete(self, *, model, system, prompt, schema, context):
        self._check_budget()
        started = time.perf_counter()
        request = {
            "model": model,
            "input": [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
            "text_format": schema,
        }
        if self.effort:
            # Reasoning-capable models accept this; the rest ignore the hint.
            request["reasoning"] = {"effort": self.effort}
        try:
            response = self.client.responses.parse(**request)
        except Exception as exc:
            if self.effort and "reasoning" in str(exc).lower():
                request.pop("reasoning", None)
                response = self.client.responses.parse(**request)
            else:
                raise

        self.usage.record(model, time.perf_counter() - started, response.usage)
        parsed = response.output_parsed
        if parsed is None:
            raise RuntimeError(f"{model} returned no parsable output")
        return parsed


def make_backend(name: str, max_calls: int | None, effort: str | None) -> Backend:
    if name == "stub":
        return StubBackend(max_calls=max_calls)
    if name == "anthropic":
        return AnthropicBackend(max_calls=max_calls, effort=effort)
    if name == "openai":
        return OpenAIBackend(max_calls=max_calls, effort=effort)
    raise SystemExit(f"unknown backend {name!r}; expected 'stub', 'anthropic' or 'openai'")
