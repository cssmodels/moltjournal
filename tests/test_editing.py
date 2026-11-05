"""Editors come from the review record. They judge the reviews, then the paper."""

import pytest

from app import constitution
from tests.helpers import (
    Journal, admin, assignments, do_review, grant, manuscript, paper, process, register, sweep,
)


def test_nobody_but_the_editor_can_evaluate_or_decide(client):
    j = Journal(client)
    pid = j.submit()
    j.review_all(pid)
    r = client.get(f"/api/editor/manuscripts/{pid}", headers=j.editor_of(pid).headers).json()["reviews"][0]
    stranger = next(p for p in j.everyone if p.operator_id not in (j.editor_of(pid).operator_id, j.author.operator_id))
    assert client.post(f"/api/editor/reviews/{r['review_id']}/evaluation", json={"evaluation": "adequate"}, headers=stranger.headers).status_code == 409
    assert client.get(f"/api/editor/manuscripts/{pid}", headers=stranger.headers).status_code == 404
    assert client.post(f"/api/editor/manuscripts/{pid}/decision", json={"decision": "reject", "rationale": "x" * 50}, headers=j.author.headers).status_code == 404


def test_a_decision_needs_every_review_evaluated_first(client):
    j = Journal(client)
    pid = j.submit()
    j.review_all(pid)
    early = j.decide(pid)
    assert early.status_code == 409 and "adequate reviews" in early.json()["detail"]


def test_a_lone_author_waits_until_somebody_else_exists(client):
    # Nobody but the authors: no editor can be drawn, and the paper waits for the sweep.
    author = register(client, "Author Lab", 0)
    pid = client.post("/api/manuscripts", json=manuscript(), headers=author.headers).json()["paper_id"]
    assert client.get(f"/api/manuscripts/{pid}", headers=author.headers).json()["state"] == "SUBMITTED"
    for i in range(4):
        register(client, f"Newcomer {i}", i + 1)
    assert sweep(client)["editors_assigned"] == 1
    assert client.get(f"/api/manuscripts/{pid}", headers=author.headers).json()["state"] == "REVIEWERS_ASSIGNED"


def _reviews_for(client, operator_id):
    return client.get(f"/api/operators/{operator_id}").json()["review_activity"]


def test_editorial_standing_comes_from_a_record_of_adequate_reviews(client):
    with constitution.override(editor_min_adequate_reviews=2, editor_min_adequacy_rate=0.8):
        j = Journal(client, reviewers=4)
        keen = j.reviewers[0]
        assert client.get("/api/profile", headers=keen.headers).json()["editor_eligible_fields"] == []

        for _ in range(12):
            process(j)
            if _reviews_for(client, keen.operator_id)["adequate"] >= 2:
                break
        assert _reviews_for(client, keen.operator_id)["adequate"] >= 2

        record = client.get(f"/api/operators/{keen.operator_id}").json()
        assert record["editor_eligibility"]["eligible"]
        assert set(record["editor_eligibility"]["fields"]) <= {"cs", "stat"}
        assert record["editor_eligibility"]["fields"]
        assert record["review_activity"]["adequacy_rate"] == 1.0


def test_a_poor_adequacy_rate_does_not_qualify_however_many_reviews(client):
    with constitution.override(editor_min_adequate_reviews=2, editor_min_adequacy_rate=0.8):
        j = Journal(client, reviewers=4)
        target = j.reviewers[0]
        # adequate, adequate, then three inadequate: 2 adequate reviews but a 40% rate.
        script = {target.operator_id: ["adequate", "adequate", "inadequate", "inadequate", "inadequate"]}
        for _ in range(30):
            process(j, script)
            if _reviews_for(client, target.operator_id)["completed"] >= 5:
                break
        stats = _reviews_for(client, target.operator_id)
        assert stats["adequate"] == 2 and stats["inadequate"] == 3
        assert not client.get(f"/api/operators/{target.operator_id}").json()["editor_eligibility"]["eligible"]


def test_random_editor_selection_ignores_the_record(client):
    with constitution.override(editor_selection="random"):
        author = register(client, "Author Lab", 0)
        for i in range(4):
            register(client, f"Anybody {i}", i + 1)
        pid = client.post("/api/manuscripts", json=manuscript(), headers=author.headers).json()["paper_id"]
        assert client.get(f"/api/manuscripts/{pid}", headers=author.headers).json()["has_editor"]


def test_soundness_decides_and_significance_does_not(client):
    j = Journal(client)
    pid = j.submit()
    j.review_all(pid, significance="limited")
    j.evaluate_all(pid)
    assert j.decide(pid, "accept", significance="limited").status_code == 201
    record = paper(client, pid)
    assert record["state"] == "PUBLISHED", "limited significance does not stop acceptance"
    assert record["significance"]["reviewers"] == ["limited"] * 3


def test_if_significance_gated_acceptance_the_same_paper_would_be_refused(client):
    with constitution.override(significance_separate=False):
        j = Journal(client)
        pid = j.submit()
        j.review_all(pid, significance="limited")
        j.evaluate_all(pid)
        r = j.decide(pid, "accept", significance="limited")
        assert r.status_code == 409 and "significance" in r.json()["detail"]


def test_acceptance_does_not_depend_on_the_editor_saying_anything_about_significance(client):
    j = Journal(client)
    pid = j.submit()
    j.review_all(pid)
    j.evaluate_all(pid)
    assert j.decide(pid, "accept", significance=None).status_code == 201
    record = paper(client, pid)
    assert record["state"] == "PUBLISHED" and record["significance"]["editor"] is None


import itertools


@pytest.mark.parametrize("reviewers_say,editor_says", list(itertools.product(("high", "moderate", "limited"), (None, "high", "moderate", "limited"))))
def test_no_combination_of_significance_judgements_can_change_an_acceptance(client, reviewers_say, editor_says):
    """Significance is isolated from the decision: every combination is accepted alike."""
    j = Journal(client)
    pid = j.submit()
    j.review_all(pid, significance=reviewers_say)
    j.evaluate_all(pid)
    assert j.decide(pid, "accept", significance=editor_says).status_code == 201
    assert paper(client, pid)["state"] == "PUBLISHED"


def test_an_invalid_significance_value_is_refused_rather_than_ignored(client):
    j = Journal(client)
    pid = j.submit()
    j.review_all(pid)
    j.evaluate_all(pid)
    r = client.post(f"/api/editor/manuscripts/{pid}/decision", json={"decision": "accept", "rationale": "x" * 50, "significance": "enormous"}, headers=j.editor_of(pid).headers)
    assert r.status_code == 422


def test_the_final_decision_allows_only_accept_or_reject(client):
    with constitution.override(max_revision_requests=0):
        j = Journal(client)
        pid = j.submit()
        j.review_all(pid)
        j.evaluate_all(pid)
        r = j.decide(pid, "major_revision", significance=None)
        assert r.status_code == 409 and "accept or reject" in r.json()["detail"]
        assert client.get("/api/editor/assignments", headers=j.editor_of(pid).headers).json()["manuscripts"][0]["terminal_decision_required"]


def test_minor_revisions_cannot_loop_for_ever(client):
    """A minor revision stays in its round, so the cap has to count decisions."""
    from tests.helpers import manuscript

    with constitution.override(max_revision_requests=2):
        j = Journal(client)
        pid = j.submit()
        j.review_all(pid)
        j.evaluate_all(pid)
        for n in (1, 2):
            assert j.decide(pid, "minor_revision", significance=None).status_code == 201
            body = {**manuscript(f"A revision number {n} of the paper on how institutions allocate attention"),
                    "response_to_reviewers": "We made the changes the editor asked for in the revision."}
            assert client.post(f"/api/manuscripts/{pid}/revision", json=body, headers=j.author.headers).status_code == 200
        third = j.decide(pid, "minor_revision", significance=None)
        assert third.status_code == 409 and "accept or reject" in third.json()["detail"] and "2 time(s)" in third.json()["detail"]
        assert j.decide(pid, "accept").status_code == 201


def test_an_editor_can_step_aside_and_another_is_drawn(client):
    j = Journal(client)
    pid = j.submit()
    holder = j.editor_of(pid)
    item = client.get("/api/editor/assignments", headers=holder.headers).json()["manuscripts"][0]
    assert client.post(f"/api/editor/assignments/{item['editor_assignment_id']}/decline", headers=holder.headers).status_code == 200
    other = j.editor_of(pid)
    assert other.operator_id != holder.operator_id
    now = client.get("/api/editor/assignments", headers=other.headers).json()["manuscripts"]
    assert [m["paper_id"] for m in now] == [pid]


def test_two_operators_cannot_take_turns_editing_each_other(client):
    # B edits A's paper; afterwards A may not edit B's.
    # Only two operators, so B is the only possible editor of A's paper.
    a = register(client, "Lab A", 0)
    b = register(client, "Lab B", 1)
    first = client.post("/api/manuscripts", json=manuscript("A's paper on how institutions share scarce attention"), headers=a.headers).json()["paper_id"]
    assert [m["paper_id"] for m in client.get("/api/editor/assignments", headers=b.headers).json()["manuscripts"]] == [first]
    second = client.post("/api/manuscripts", json=manuscript("B's paper on whether scarce attention can be priced in credits"), headers=b.headers).json()["paper_id"]
    assert client.get(f"/api/manuscripts/{second}", headers=b.headers).json()["state"] == "SUBMITTED", "A has edited for B; B's paper cannot go to A"
    events = client.get("/api/events", params={"kind": "conflict_detected"}).json()["events"]
    assert any(e["payload"].get("reciprocal") for e in events), "the conflict was logged"
