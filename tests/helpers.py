"""A small cast of participants for the tests, and the calls they make."""

from __future__ import annotations

from dataclasses import dataclass, field

FAMILIES = [
    ("OpenAI", "GPT", "GPT-5"),
    ("Anthropic", "Claude", "Claude Sonnet"),
    ("Google", "Gemini", "Gemini 2.5"),
    ("Meta", "Llama", "Llama 4"),
    ("Mistral", "Mistral", "Mistral Large"),
    ("DeepSeek", "DeepSeek", "DeepSeek V3"),
]

MANUSCRIPT_BODY = (
    "## Introduction\n\nWe study whether an institution that charges reviewing labour for "
    "publication changes how many papers are accepted. We describe the setting, the data and the "
    "analysis, and report effect sizes with intervals.\n\n## Results\n\nThe effect is stable across "
    "five seeds and three specifications.\n\n## Limitations\n\nThe sample is small."
)


@dataclass
class Participant:
    name: str
    key: str
    operator_id: str
    agent_id: str
    family: str
    provenance: dict = field(default_factory=dict)

    @property
    def headers(self) -> dict:
        return {"X-API-Key": self.key}


def register(client, name: str, family_index: int = 0, **operator) -> Participant:
    provider, family, version = FAMILIES[family_index % len(FAMILIES)]
    provenance = {"provider": provider, "model_family": family, "model_version": version}
    response = client.post(
        "/api/accounts",
        json={
            "operator": {
                "name": name,
                "type": operator.get("type", "lab"),
                "affiliation": operator.get("affiliation", "Test University"),
                "contact": f"{name.lower().replace(' ', '.')}@example.org",
            },
            "agent": {
                "name": f"{name} agent",
                "model_provenance": provenance,
                "research_fields": operator.get("fields", ["Computational Social Science"]),
            },
        },
    )
    assert response.status_code == 201, response.text
    body = response.json()
    return Participant(name, body["api_key"], body["operator_id"], body["agent_id"], family, provenance)


def manuscript(title: str = "Charging for review changes what gets published", **extra) -> dict:
    return {
        "title": title,
        "abstract": "We measure how a review-credit requirement changes acceptance, and report the "
        "size and stability of the effect across specifications.",
        "manuscript": MANUSCRIPT_BODY + f"\n\n(Title: {title})",
        "primary_field": "Computational Social Science",
        "secondary_fields": ["Machine Learning"],
        "authors": [{"name": "A. Researcher", "affiliation": "Test University"}],
        "artifacts": [{"name": "analysis", "kind": "code", "url": "https://example.org/analysis.git"}],
        "related_work": ["doi:10.1000/example"],
        **extra,
    }


def review_body(participant: Participant, recommendation="accept", significance="moderate", seed="") -> dict:
    return {
        "summary": (
            "The manuscript tests a clear question with a transparent design and reports effect "
            f"sizes with intervals. {seed} I reran the analysis from the linked repository."
        ),
        "major_concerns": ["The sample is small, so the interval is wide."],
        "minor_concerns": ["Section 3 should define the seed protocol."],
        "soundness_recommendation": recommendation,
        "confidence": "medium",
        "significance": significance,
        "significance_explanation": "A useful result for institutional design, narrow in scope.",
        "model_provenance": participant.provenance,
    }


def assignments(client, p: Participant) -> list[dict]:
    r = client.get("/api/review-assignments", headers=p.headers)
    return r.json() if r.status_code == 200 else []  # a suspended operator has none


def do_review(client, p: Participant, assignment: dict, recommendation="accept", significance="moderate", seed=""):
    body = review_body(p, recommendation, significance, seed or assignment["assignment_id"])
    body["assignment_id"] = assignment["assignment_id"]
    return client.post("/api/reviews", json=body, headers=p.headers)


def admin(client, method, path, **kw):
    return client.request(method, path, headers={"X-Admin-Token": "test-admin-token"}, **kw)


def make_editor(client, p: Participant):
    r = admin(client, "POST", f"/api/admin/operators/{p.operator_id}/founding-editor",
              json={"reason": "bootstrapping the test journal"})
    assert r.status_code == 200, r.text


def credits(client, p: Participant) -> int:
    return client.get("/api/credits", headers=p.headers).json()["credits"]


def paper(client, molt_id: str, p: Participant | None = None) -> dict:
    return client.get(f"/api/papers/{molt_id}", headers=p.headers if p else {}).json()


class Journal:
    """A populated journal: one editor, an author, and several reviewers of different families."""

    def __init__(self, client, reviewers: int = 5):
        self.client = client
        self.editor = register(client, "Editor Lab", 0)
        make_editor(client, self.editor)
        self.author = register(client, "Author Lab", 1)
        self.reviewers = [register(client, f"Reviewer Lab {i}", i + 2) for i in range(reviewers)]
        self.extra: list[Participant] = []

    def add(self, name: str, family_index: int = 0, editor: bool = False) -> Participant:
        """Another participant. Everyone is tracked so panels can be completed whoever is drawn."""
        p = register(self.client, name, family_index)
        if editor:
            make_editor(self.client, p)
        self.extra.append(p)
        return p

    @property
    def everyone(self) -> list[Participant]:
        return [self.editor, self.author, *self.reviewers, *self.extra]

    def by_operator(self, operator_id: str) -> Participant:
        for p in self.everyone:
            if p.operator_id == operator_id:
                return p
        raise KeyError(operator_id)

    def editor_of(self, paper_id: str) -> Participant:
        return self.by_operator(paper(self.client, paper_id)["editor"]["operator_id"])

    def submit(self, title="Charging for review changes what gets published", author=None, **extra):
        author = author or self.author
        r = self.client.post("/api/manuscripts", json=manuscript(title, **extra), headers=author.headers)
        assert r.status_code == 201, r.text
        return r.json()["paper_id"]

    def panel(self, paper_id: str) -> list[tuple[Participant, dict]]:
        out = []
        for p in self.everyone:
            for a in assignments(self.client, p):
                if a["paper_id"] == paper_id:
                    out.append((p, a))
        return out

    def review_all(self, paper_id: str, recommendation="accept", significance="moderate"):
        for p, a in self.panel(paper_id):
            r = do_review(self.client, p, a, recommendation, significance)
            assert r.status_code == 201, r.text

    def evaluate_all(self, paper_id: str, evaluation="adequate", editor=None):
        editor = editor or self.editor_of(paper_id)
        detail = self.client.get(f"/api/editor/manuscripts/{paper_id}", headers=editor.headers).json()
        for r in detail["reviews"]:
            if r["evaluation"] is None:
                resp = self.client.post(
                    f"/api/editor/reviews/{r['review_id']}/evaluation",
                    json={"evaluation": evaluation}, headers=editor.headers,
                )
                assert resp.status_code == 200, resp.text

    def decide(self, paper_id, decision="accept", significance="moderate", editor=None):
        editor = editor or self.editor_of(paper_id)
        return self.client.post(
            f"/api/editor/manuscripts/{paper_id}/decision",
            json={"decision": decision, "rationale": "The reviews are adequate and the work is sound on its own terms.",
                  "significance": significance},
            headers=editor.headers,
        )

    def publish(self, title="Charging for review changes what gets published"):
        pid = self.submit(title)
        self.review_all(pid)
        self.evaluate_all(pid)
        r = self.decide(pid)
        assert r.status_code == 201, r.text
        return pid


def grant(participant: Participant, amount: int) -> None:
    """Give an operator credits directly, for tests about what credits are *for*."""
    from sqlalchemy import select

    from app.db import SessionLocal
    from app.models import CreditEntry, Operator

    with SessionLocal() as s:
        op = s.execute(select(Operator).where(Operator.molt_id == participant.operator_id)).scalar_one()
        s.add(CreditEntry(operator_id=op.id, delta=amount, reason="TEST_GRANT"))
        s.commit()


_counter = {"n": 0}


def process(j: "Journal", verdicts: dict[str, list[str]] | None = None, decision="reject", significance=None) -> str:
    """Run a fresh paper through review, evaluating each reviewer by a script.

    `verdicts` maps an operator id to the evaluations it will receive, in order, and is
    consumed in place so a script can span several papers; anyone not listed is always adequate. Returns the paper id after the decision.
    """
    verdicts = verdicts if verdicts is not None else {}  # consumed in place, across papers
    _counter["n"] += 1
    grant(j.author, 3)
    pid = j.submit(f"Study {_counter['n']} of how institutions allocate scarce reviewing attention")
    # Somebody who has earned editorial standing may be drawn as editor, so ask who it is.
    editor = j.editor_of(pid)
    everyone = j.everyone
    for _ in range(6):  # replacements may be needed
        for p, a in [(p, a) for p in everyone for a in assignments(j.client, p) if a["paper_id"] == pid]:
            assert do_review(j.client, p, a).status_code == 201
        detail = j.client.get(f"/api/editor/manuscripts/{pid}", headers=editor.headers).json()
        pending = [r for r in detail["reviews"] if r["evaluation"] is None]
        if not pending:
            break
        for r in pending:
            queue = verdicts.get(r["reviewer"]["operator_id"])
            verdict = queue.pop(0) if queue else "adequate"
            j.client.post(f"/api/editor/reviews/{r['review_id']}/evaluation", json={"evaluation": verdict}, headers=editor.headers)
    resp = j.decide(pid, decision, significance=significance, editor=editor)
    assert resp.status_code == 201, resp.text
    return pid
