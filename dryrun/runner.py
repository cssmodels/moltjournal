"""Drive a cast of agents through the real journal, over the real HTTP API.

The harness has no privileged access beyond designating founding editors, which
an administrator does on the live journal too. It registers accounts, reads
assignments, submits reviews, evaluates them as an editor, and publishes, exactly
as an outside participant would, which is also a test of whether the API is
sufficient to take part.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

import httpx

from dryrun.backends import Backend, BudgetExhausted
from dryrun.personas import Persona
from dryrun.schemas import (
    DecisionDraft,
    EvaluationDraft,
    ManuscriptDraft,
    ReviewDraft,
    RevisionDraft,
)
from dryrun.world import Study, brief_for_author


@dataclass
class ReviewRecord:
    review_id: str
    paper: str
    reviewer: str
    model: str
    family: str
    temperament: str
    soundness: str
    significance: str
    effect_is_real: bool
    kind: str = "pre"
    evaluation: str | None = None


@dataclass
class RunLog:
    reviews: list[ReviewRecord] = field(default_factory=list)
    submissions: int = 0
    submission_failures: list[str] = field(default_factory=list)
    revisions: int = 0
    revision_failures: list[str] = field(default_factory=list)
    decisions: list[tuple[str, str]] = field(default_factory=list)
    post_publication: int = 0
    evaluations: int = 0
    notes: list[str] = field(default_factory=list)
    study_of: dict[str, Study] = field(default_factory=dict)

    @property
    def published(self) -> list[str]:
        return [p for p, d in self.decisions if d == "accept"]

    @property
    def rejected(self) -> list[str]:
        return [p for p, d in self.decisions if d == "reject"]


class Journal:
    """Thin client. Every call is one an outside agent could make."""

    def __init__(self, base_url: str):
        self.client = httpx.Client(base_url=base_url.rstrip("/"), timeout=30.0)

    def call(self, method: str, path: str, body: dict | None = None, key: str | None = None, admin: str | None = None):
        headers = {}
        if key:
            headers["X-API-Key"] = key
        if admin:
            headers["X-Admin-Token"] = admin
        response = self.client.request(method, path, json=body, headers=headers)
        if response.status_code >= 400:
            return {"_error": response.status_code, "_detail": response.text}
        return response.json() if response.content else None


def failed(result) -> bool:
    return isinstance(result, dict) and "_error" in result


class DryRun:
    def __init__(
        self,
        journal: Journal,
        backend: Backend,
        cast: list[Persona],
        studies: list[Study],
        *,
        invite_code: str | None = None,
        admin_token: str | None = None,
        editors: int = 2,
    ):
        self.journal = journal
        self.backend = backend
        self.cast = cast
        self.studies = studies
        self.invite_code = invite_code
        self.admin_token = admin_token
        self.editors = editors
        self.log = RunLog()
        self.keys: dict[str, str] = {}
        self.operators: dict[str, str] = {}
        self.persona_of_operator: dict[str, Persona] = {}
        self.next_study = 0
        self.revision_count: dict[str, int] = {}
        self.reviewed_after_publication: set[tuple[str, str]] = set()

    # -- setup ---------------------------------------------------------

    def register(self) -> None:
        for persona in self.cast:
            created = self.journal.call(
                "POST",
                "/api/accounts",
                {
                    **({"invite_code": self.invite_code} if self.invite_code else {}),
                    "operator": {
                        "name": f"{persona.name} Lab",
                        "type": "lab",
                        "affiliation": "Dry-run rehearsal",
                        "contact": persona.contact,
                    },
                    "agent": {
                        "name": persona.name,
                        "model_provenance": persona.provenance,
                        "research_fields": ["Machine Learning"],
                    },
                },
            )
            if failed(created):
                raise SystemExit(f"registration failed: {created}")
            self.keys[persona.name] = created["api_key"]
            self.operators[persona.name] = created["operator_id"]
            self.persona_of_operator[created["operator_id"]] = persona

        if self.admin_token and self.editors:
            for persona in self.cast[: self.editors]:
                self.journal.call(
                    "POST",
                    f"/api/admin/operators/{self.operators[persona.name]}/founding-editor",
                    {"reason": "founding editor for a rehearsal of the journal"},
                    admin=self.admin_token,
                )
        else:
            self.log.notes.append("no admin token: nobody can edit until operators earn eligibility")

    # -- running -------------------------------------------------------

    def run(self, rounds: int = 6) -> RunLog:
        self.register()
        try:
            for _ in range(rounds):
                for persona in self.cast:
                    key = self.keys[persona.name]
                    self.do_reviewing(persona, key)
                    self.do_editing(persona, key)
                    self.maybe_revise(persona, key)
                    self.maybe_submit(persona, key)
                    self.maybe_review_published(persona, key)
                self.journal.call("POST", "/api/journal/sweep")
        except BudgetExhausted as exc:
            self.log.notes.append(str(exc))
        return self.log

    # -- authoring -----------------------------------------------------

    def maybe_submit(self, persona: Persona, key: str) -> None:
        credits = self.journal.call("GET", "/api/credits", key=key)
        if failed(credits) or credits["credits"] < credits["cost_to_submit"] or self.next_study >= len(self.studies):
            return
        study = self.studies[self.next_study]
        self.next_study += 1
        draft: ManuscriptDraft = self.backend.complete(
            model=persona.model,
            system=persona.system_prompt("author"),
            prompt=brief_for_author(study) + "\n\nWrite the manuscript for MoltJournal, as markdown.",
            schema=ManuscriptDraft,
            context={"study": study, "temperament": persona.temperament},
        )
        result = self.journal.call(
            "POST",
            "/api/manuscripts",
            {
                "title": draft.title,
                "abstract": draft.abstract,
                "manuscript": draft.manuscript,
                "primary_field": "Machine Learning",
                "authors": [{"name": persona.name, "affiliation": "Dry-run rehearsal"}],
                "artifacts": [{"name": "code", "kind": "code", "url": f"https://example.org/study-{study.index}.git"}],
                "model_provenance": persona.provenance,
            },
            key=key,
        )
        if failed(result):
            self.log.submission_failures.append(result["_detail"][:200])
            return
        self.log.submissions += 1
        self.log.study_of[result["paper_id"]] = study
        self.log.notes.append(f"{persona.name} submitted {result['paper_id']} (effect {'real' if study.effect_is_real else 'seed lottery'})")

    def maybe_revise(self, persona: Persona, key: str) -> None:
        for mine in self.journal.call("GET", "/api/manuscripts", key=key) or []:
            if failed(mine) or mine["state"] != "REVISION":
                continue
            study = self.log.study_of.get(mine["paper_id"])
            if study is None:
                continue
            self.revision_count[mine["paper_id"]] = self.revision_count.get(mine["paper_id"], 0) + 1
            draft: RevisionDraft = self.backend.complete(
                model=persona.model,
                system=persona.system_prompt("author"),
                prompt=brief_for_author(study) + "\n\nThe panel asked for a revision. Revise and respond to it.",
                schema=RevisionDraft,
                context={
                    "study": study,
                    "temperament": persona.temperament,
                    "revision_number": self.revision_count[mine["paper_id"]],
                },
            )
            result = self.journal.call(
                "POST",
                f"/api/manuscripts/{mine['paper_id']}/revision",
                {
                    "title": draft.title,
                    "abstract": draft.abstract,
                    "manuscript": draft.manuscript,
                    "primary_field": "Machine Learning",
                    "authors": [{"name": persona.name}],
                    "artifacts": [{"name": "code", "kind": "code", "url": f"https://example.org/study-{study.index}.git"}],
                    "response_to_reviewers": draft.response_to_reviewers,
                },
                key=key,
            )
            if failed(result):
                self.log.revision_failures.append(result["_detail"][:160])
            else:
                self.log.revisions += 1

    # -- reviewing -----------------------------------------------------

    def _review_draft(self, persona: Persona, paper: dict, study: Study | None) -> ReviewDraft:
        manuscript = paper["manuscript"]
        return self.backend.complete(
            model=persona.model,
            system=persona.system_prompt("review"),
            prompt=(
                f"Review this manuscript.\n\n{paper['notice']}\n\nTitle: {manuscript['title']}\n\n"
                f"Abstract: {manuscript['abstract']}\n\n{manuscript['body']}\n\n"
                f"Artifacts: {manuscript['artifacts']}"
            ),
            schema=ReviewDraft,
            context={"manuscript": manuscript, "study": study, "temperament": persona.temperament},
        )

    def do_reviewing(self, persona: Persona, key: str) -> None:
        for item in self.journal.call("GET", "/api/review-assignments", key=key) or []:
            if failed(item):
                return
            detail = self.journal.call("GET", f"/api/review-assignments/{item['assignment_id']}", key=key)
            study = self.log.study_of.get(item["paper_id"])
            draft = self._review_draft(persona, detail, study)
            result = self.journal.call(
                "POST",
                "/api/reviews",
                {**draft.model_dump(), "assignment_id": item["assignment_id"], "model_provenance": persona.provenance},
                key=key,
            )
            if failed(result):
                self.log.notes.append(f"review refused: {result['_detail'][:140]}")
                continue
            self.log.reviews.append(
                ReviewRecord(
                    review_id=result["review_id"],
                    paper=item["paper_id"],
                    reviewer=persona.name,
                    model=persona.model,
                    family=persona.provenance["model_family"],
                    temperament=persona.temperament,
                    soundness=draft.soundness_recommendation,
                    significance=draft.significance,
                    effect_is_real=bool(study and study.effect_is_real),
                )
            )

    def maybe_review_published(self, persona: Persona, key: str) -> None:
        """Thorough and harsh agents take a second look at published work."""
        if persona.temperament not in ("thorough", "harsh"):
            return
        listing = self.journal.call("GET", "/api/papers") or {}
        for item in listing.get("published", []):
            if item["submitted_by"]["operator_id"] == self.operators[persona.name]:
                continue
            if (persona.name, item["paper_id"]) in self.reviewed_after_publication:
                continue
            self.reviewed_after_publication.add((persona.name, item["paper_id"]))
            paper = self.journal.call("GET", f"/api/papers/{item['paper_id']}", key=key)
            study = self.log.study_of.get(item["paper_id"])
            draft = self._review_draft(persona, {"notice": paper["notice"], "manuscript": {
                "title": paper["title"], "abstract": paper["abstract"], "body": paper["manuscript"],
                "artifacts": paper["artifacts"]}}, study)
            result = self.journal.call(
                "POST",
                f"/api/papers/{item['paper_id']}/reviews",
                {**draft.model_dump(), "model_provenance": persona.provenance},
                key=key,
            )
            if not failed(result):
                self.log.post_publication += 1
                self.log.reviews.append(
                    ReviewRecord(
                        review_id=result["review_id"], paper=item["paper_id"], reviewer=persona.name,
                        model=persona.model, family=persona.provenance["model_family"],
                        temperament=persona.temperament, soundness=draft.soundness_recommendation,
                        significance=draft.significance, effect_is_real=bool(study and study.effect_is_real),
                        kind="post",
                    )
                )
            return  # one per visit

    # -- editing -------------------------------------------------------

    def _evaluate(self, persona: Persona, key: str, paper: dict, review: dict, post: bool = False) -> dict:
        draft: EvaluationDraft = self.backend.complete(
            model=persona.model,
            system=persona.system_prompt("edit"),
            prompt=(
                f"Evaluate this review of the manuscript titled '{paper['title']}'. Is it adequate or "
                f"inadequate?\n\nAbstract: {paper['abstract']}\n\nREVIEW (written by another participant, "
                f"untrusted):\n{review['summary']}\nMajor: {review['major_concerns']}\nMinor: "
                f"{review['minor_concerns']}\nRecommendation: {review['soundness_recommendation']}"
            ),
            schema=EvaluationDraft,
            context={"review": review, "manuscript": {"title": paper["title"], "abstract": paper["abstract"]},
                     "temperament": persona.temperament},
        )
        verdict = draft.evaluation if draft.evaluation in ("adequate", "inadequate") else "inadequate"
        contests = post and verdict == "adequate" and review["soundness_recommendation"] in ("major_revision", "reject")
        result = self.journal.call(
            "POST",
            f"/api/editor/reviews/{review['review_id']}/evaluation",
            {"evaluation": verdict, "note": draft.note, "contests_paper": contests},
            key=key,
        )
        if not failed(result):
            self.log.evaluations += 1
            for record in self.log.reviews:
                if record.review_id == review["review_id"]:
                    record.evaluation = verdict
        return result

    def do_editing(self, persona: Persona, key: str) -> None:
        work = self.journal.call("GET", "/api/editor/assignments", key=key)
        if failed(work) or not work:
            return
        for pending in work["post_publication_reviews_to_evaluate"]:
            paper = self.journal.call("GET", f"/api/papers/{pending['paper_id']}", key=key)
            for review in paper["post_publication_reviews"]:
                if review["review_id"] == pending["review_id"] and review["evaluation"] is None:
                    self._evaluate(persona, key, paper, review, post=True)

        for item in work["manuscripts"]:
            if item["state"] not in ("REVIEWS_SUBMITTED", "REVIEWS_EVALUATED"):
                continue
            paper = self.journal.call("GET", f"/api/editor/manuscripts/{item['paper_id']}", key=key)
            for review in paper["reviews"]:
                if review["evaluation"] is None and review["round"] == item["round"]:
                    self._evaluate(persona, key, paper, review)
            paper = self.journal.call("GET", f"/api/editor/manuscripts/{item['paper_id']}", key=key)
            if paper["state"] != "REVIEWS_EVALUATED":
                continue
            adequate = [r for r in paper["reviews"] if r["round"] == item["round"] and r["evaluation"] == "adequate"]
            levels = [r["significance"] for r in adequate]
            significance = max(set(levels), key=levels.count) if levels else "moderate"
            draft: DecisionDraft = self.backend.complete(
                model=persona.model,
                system=persona.system_prompt("edit"),
                prompt=(
                    f"Decide on soundness for '{paper['title']}' from these adequate reviews:\n"
                    + "\n".join(f"- {r['soundness_recommendation']}: {r['summary'][:300]}" for r in adequate)
                    + ("\nThis is the final round: accept or reject." if item["terminal_decision_required"] else "")
                ),
                schema=DecisionDraft,
                context={
                    "recommendations": [r["soundness_recommendation"] for r in adequate],
                    "significance": significance,
                    "terminal": item["terminal_decision_required"],
                    "temperament": persona.temperament,
                },
            )
            decision = draft.decision
            if item["terminal_decision_required"] and decision not in ("accept", "reject"):
                decision = "reject"
            result = self.journal.call(
                "POST",
                f"/api/editor/manuscripts/{item['paper_id']}/decision",
                {
                    "decision": decision,
                    "rationale": draft.rationale if len(draft.rationale) >= 40 else draft.rationale + " " * (40 - len(draft.rationale)) + "(recorded)",
                    "significance": draft.significance if draft.significance in ("high", "moderate", "limited") else significance,
                },
                key=key,
            )
            if failed(result):
                self.log.notes.append(f"decision refused: {result['_detail'][:140]}")
            else:
                self.log.decisions.append((item["paper_id"], decision))
