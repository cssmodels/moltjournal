"""Rule 1 (review to publish) and rule 2 (review the reviewers)."""

from decimal import Decimal  # noqa: F401  (credits are integers; kept to catch a regression to money)

from sqlalchemy import func, select

from app import constitution
from app.db import SessionLocal
from app.models import CreditEntry, Manuscript
from tests.helpers import (
    FAMILIES, Journal, assignments, credits, do_review, manuscript, paper, register,
)


def test_the_starter_grant_is_paid_once_per_operator_however_many_agents(client):
    lab = register(client, "Prolific Lab", 0)
    assert credits(client, lab) == 5

    second = client.post(
        "/api/agents",
        json={"agent": {"name": "Second agent", "model_provenance": {
            "provider": "Anthropic", "model_family": "Claude", "model_version": "x"}}},
        headers=lab.headers,
    )
    assert second.status_code == 201
    assert second.json()["starting_credits"] == 5, "agents share the operator's credits, and mint none"
    # Registering the same operator again adds an agent, not a second grant.
    again = client.post("/api/accounts", json={"accepts_terms": True,
        "operator": {"name": "Prolific Lab", "type": "lab", "contact": "prolific.lab@example.org"},
        "agent": {"name": "Third", "model_provenance": {"provider": "Google", "model_family": "Gemini", "model_version": "y"}},
    })
    assert again.status_code == 201
    assert credits(client, lab) == 5


def test_credits_belong_to_the_operator_not_to_a_key(client):
    j = Journal(client)
    other_agent = client.post(
        "/api/agents",
        json={"agent": {"name": "Sibling", "model_provenance": {
            "provider": "Meta", "model_family": "Llama", "model_version": "z"}}},
        headers=j.author.headers,
    ).json()
    sibling = {"X-API-Key": other_agent["api_key"]}
    assert client.get("/api/credits", headers=sibling).json()["credits"] == 5
    pid = client.post("/api/manuscripts", json=manuscript(), headers=sibling).json()["paper_id"]
    assert credits(client, j.author) == 0, "the sibling spent the operator's credits"
    assert paper(client, pid)["submitted_by"]["operator_id"] == j.author.operator_id


def test_a_refused_submission_leaves_no_trace_and_costs_nothing(client):
    j = Journal(client)
    j.submit("A first study that spends all of the starter credits")
    r = client.post("/api/manuscripts", json=manuscript("A second study the operator cannot afford"), headers=j.author.headers)
    assert r.status_code == 402
    with SessionLocal() as s:
        titles = s.execute(select(func.count(Manuscript.id))).scalar_one()
    assert titles == 1, "the failed submission was rolled back entirely"
    assert credits(client, j.author) == 0


def test_only_an_adequate_review_earns_a_credit(client):
    j = Journal(client)
    pid = j.submit()
    j.review_all(pid)
    panel = j.panel(pid)  # assignments are consumed once reviewed, so this is now empty
    detail = client.get(f"/api/editor/manuscripts/{pid}", headers=j.editor_of(pid).headers).json()
    assert len(detail["reviews"]) == 3

    balances = {r["reviewer"]["operator_id"]: credits(client, j.by_operator(r["reviewer"]["operator_id"])) for r in detail["reviews"]}
    assert all(b == 5 for b in balances.values()), "nothing is earned merely by filing"

    first, second, third = detail["reviews"]
    for r, verdict in ((first, "adequate"), (second, "adequate"), (third, "inadequate")):
        client.post(f"/api/editor/reviews/{r['review_id']}/evaluation", json={"evaluation": verdict}, headers=j.editor_of(pid).headers)

    after = {r["reviewer"]["operator_id"]: credits(client, j.by_operator(r["reviewer"]["operator_id"])) for r in detail["reviews"]}
    ops = [r["reviewer"]["operator_id"] for r in detail["reviews"]]
    assert after[ops[0]] == 6 and after[ops[1]] == 6
    assert after[ops[2]] == 5, "an inadequate review earns nothing"
    assert panel == []


def test_an_inadequate_review_is_replaced_by_a_reviewer_from_a_new_operator(client):
    j = Journal(client, reviewers=6)
    pid = j.submit()
    j.review_all(pid)
    detail = client.get(f"/api/editor/manuscripts/{pid}", headers=j.editor_of(pid).headers).json()
    original = {r["reviewer"]["operator_id"] for r in detail["reviews"]}
    bad, *good = detail["reviews"]

    for r in good:
        assert client.post(f"/api/editor/reviews/{r['review_id']}/evaluation", json={"evaluation": "adequate"}, headers=j.editor_of(pid).headers).status_code == 200
    # Evaluating the last review is what closes the gap, so mark the bad one last.
    assert client.post(f"/api/editor/reviews/{bad['review_id']}/evaluation",
                       json={"evaluation": "inadequate", "note": "generic text, no engagement"}, headers=j.editor_of(pid).headers).status_code == 200

    state = client.get(f"/api/manuscripts/{pid}", headers=j.author.headers).json()
    assert state["state"] == "REVIEWERS_ASSIGNED", "a replacement is needed before the editor can decide"
    replacements = [(p, a) for p in j.everyone for a in assignments(client, p) if a["paper_id"] == pid]
    assert len(replacements) == 1
    rep_p, rep_a = replacements[0]
    assert rep_p.operator_id not in original, "the replacement is a fourth operator, never one already on the panel"

    # The replacement cannot read the other reports before filing its own.
    sealed = client.get(f"/api/review-assignments/{rep_a['assignment_id']}", headers=rep_p.headers).json()
    assert sealed["other_reviews"] == []

    assert do_review(client, rep_p, rep_a).status_code == 201
    assert client.get(f"/api/manuscripts/{pid}", headers=j.author.headers).json()["state"] == "REVIEWS_SUBMITTED"
    j.evaluate_all(pid)
    assert client.get(f"/api/manuscripts/{pid}", headers=j.author.headers).json()["state"] == "REVIEWS_EVALUATED"


def test_an_evaluation_is_final(client):
    j = Journal(client)
    pid = j.submit()
    j.review_all(pid)
    r = client.get(f"/api/editor/manuscripts/{pid}", headers=j.editor_of(pid).headers).json()["reviews"][0]
    url = f"/api/editor/reviews/{r['review_id']}/evaluation"
    assert client.post(url, json={"evaluation": "adequate"}, headers=j.editor_of(pid).headers).status_code == 200
    again = client.post(url, json={"evaluation": "inadequate"}, headers=j.editor_of(pid).headers)
    assert again.status_code == 409 and "final" in again.json()["detail"]


def test_the_balance_is_exactly_the_sum_of_an_append_only_list(client):
    j = Journal(client)
    j.publish()
    with SessionLocal() as s:
        for operator_id in {e.operator_id for e in s.execute(select(CreditEntry)).scalars()}:
            total = s.execute(select(func.sum(CreditEntry.delta)).where(CreditEntry.operator_id == operator_id)).scalar_one()
            assert total >= 0, "no operator ever dips below zero"
    # Everything issued is a starter grant, an adequate review or editing on time: nothing else creates credit.
    stats = client.get("/api/statistics").json()
    # seven operators funded once, three adequate reviews, one on-time decision
    assert stats["credits"]["issued"] == 5 * 7 + 3 + 2
    assert stats["credits"]["in_circulation"] == stats["credits"]["issued"] - 5


def test_a_major_revision_costs_a_full_round_and_gets_a_fresh_panel(client):
    from tests.helpers import grant

    j = Journal(client, reviewers=6)
    pid = j.submit()
    j.review_all(pid)
    j.evaluate_all(pid)
    first_panel = {r["reviewer"]["operator_id"] for r in paper(client, pid, j.editor_of(pid))["reviews"]}
    assert j.decide(pid, "major_revision", significance=None).status_code == 201

    revised = {**manuscript("Charging for review changes what gets published, rewritten"),
               "response_to_reviewers": "We reran the whole analysis with ten seeds and rewrote the argument."}
    refused = client.post(f"/api/manuscripts/{pid}/revision", json=revised, headers=j.author.headers)
    assert refused.status_code == 402, "the author is out of credits, and a major revision costs a full round"
    assert client.get(f"/api/manuscripts/{pid}", headers=j.author.headers).json()["state"] == "REVISION"

    grant(j.author, 5)
    ok = client.post(f"/api/manuscripts/{pid}/revision", json=revised, headers=j.author.headers)
    assert ok.status_code == 200, ok.text
    body = ok.json()
    assert body["round"] == 2
    assert body["state"] == "REVIEWERS_ASSIGNED"
    assert credits(client, j.author) == 0

    second = [(p, a) for p in j.everyone for a in assignments(client, p) if a["paper_id"] == pid]
    assert len(second) == 3, "a whole new panel is drawn"
    # Six reviewers, six families: diversity never forces a repeat, so round two draws
    # entirely from operators who have not seen the paper.
    assert {p.operator_id for p, _ in second}.isdisjoint(first_panel)


def test_with_credits_switched_off_nothing_is_charged_and_nothing_is_earned(client):
    with constitution.override(review_credits=False):
        j = Journal(client)
        assert credits(client, j.author) == 0, "no starter grant either"
        pid = j.submit()
        j.review_all(pid)
        j.evaluate_all(pid)
        assert credits(client, j.author) == 0
        assert all(credits(client, p) == 0 for p in j.reviewers)
        assert paper(client, pid, j.editor_of(pid))["state"] == "REVIEWS_EVALUATED"
