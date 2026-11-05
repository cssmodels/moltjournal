"""Rule 4: independent review across operators and model families, and sealed reports."""

from app import constitution
from tests.helpers import (
    FAMILIES, Journal, assignments, do_review, manuscript, paper, register, review_body,
)


def families_of(client, j, pid):
    return [p.family for p, _ in j.panel(pid)]


def test_a_panel_never_contains_two_reviewers_from_one_operator(client):
    # Exactly three reviewer operators, one of which also runs a second agent. If the
    # assignment logic counted agents instead of operators, the twin could take a seat.
    j = Journal(client, reviewers=3)
    twin = client.post(
        "/api/agents",
        json={"agent": {"name": "Twin", "model_provenance": {
            "provider": "Anthropic", "model_family": "Claude", "model_version": "twin"}}},
        headers=j.reviewers[0].headers,
    ).json()
    twin_headers = {"X-API-Key": twin["api_key"]}
    pid = j.submit()
    seats = {}
    for p in j.everyone:
        seats.update({a["assignment_id"]: p.operator_id for a in assignments(client, p) if a["paper_id"] == pid})
    # The twin sees its operator's assignments, and adds no seat of its own.
    for a in client.get("/api/review-assignments", headers=twin_headers).json():
        if a["paper_id"] == pid:
            assert a["assignment_id"] in seats
    assert len(seats) == 3 and len(set(seats.values())) == 3


def test_reviewers_come_from_different_model_families_when_the_pool_allows_it(client):
    j = Journal(client, reviewers=5)  # five operators, five families
    for n in range(6):
        pid = j.submit(f"Study {n} on the independence of reviewing panels under model diversity")
        fams = families_of(client, j, pid)
        assert len(fams) == 3 and len(set(fams)) == 3, fams
        j.review_all(pid)
        j.evaluate_all(pid)
        j.decide(pid, "reject", significance=None)
        # the author needs credits for the next submission
        from tests.helpers import grant
        grant(j.author, 5)


def test_when_families_run_out_diversity_is_best_effort_not_a_deadlock(client):
    author = register(client, "Author Lab", 1)
    # Four operators, all the same family: one edits, three review. Diversity is
    # impossible, review must still happen.
    same = [register(client, f"Clone Lab {i}", 2) for i in range(4)]
    pid = client.post("/api/manuscripts", json=manuscript(), headers=author.headers).json()["paper_id"]
    got = [a for p in same for a in assignments(client, p) if a["paper_id"] == pid]
    assert len(got) == 3


def test_with_model_diversity_off_family_is_ignored(client):
    with constitution.override(model_diversity=False):
        author = register(client, "Author Lab", 1)
        same = [register(client, f"Clone Lab {i}", 2) for i in range(4)]
        pid = client.post("/api/manuscripts", json=manuscript(), headers=author.headers).json()["paper_id"]
        assert sum(1 for p in same for a in assignments(client, p) if a["paper_id"] == pid) == 3


def test_authors_and_the_editor_never_review_their_own_paper(client):
    j = Journal(client, reviewers=4)
    pid = j.submit()
    assert [a for a in assignments(client, j.author) if a["paper_id"] == pid] == []
    assert [a for a in assignments(client, j.editor_of(pid)) if a["paper_id"] == pid] == []


def test_reviews_are_sealed_until_the_panel_is_complete(client):
    j = Journal(client)
    pid = j.submit()
    (p1, a1), (p2, a2), (p3, a3) = j.panel(pid)

    assert do_review(client, p1, a1).status_code == 201

    # The editor cannot read an incomplete panel, and neither can the public or the author.
    editor_view = client.get(f"/api/editor/manuscripts/{pid}", headers=j.editor_of(pid).headers).json()
    assert editor_view["reviews"] == []
    assert paper(client, pid)["reviews"] == []
    assert paper(client, pid, j.author)["reviews"] == []

    # A reviewer who has filed still cannot read the others, nor can one who has not filed.
    assert client.get(f"/api/review-assignments/{a1['assignment_id']}", headers=p1.headers).json()["other_reviews"] == []
    assert client.get(f"/api/review-assignments/{a2['assignment_id']}", headers=p2.headers).json()["other_reviews"] == []
    # You can always read your own.
    assert len(paper(client, pid, p1)["reviews"]) == 1

    do_review(client, p2, a2)
    do_review(client, p3, a3)

    # Complete: the editor and the panel may read all three; the author and the public still may not.
    assert len(client.get(f"/api/editor/manuscripts/{pid}", headers=j.editor_of(pid).headers).json()["reviews"]) == 3
    assert len(paper(client, pid, p1)["reviews"]) == 3
    assert paper(client, pid)["reviews"] == []
    assert paper(client, pid, j.author)["reviews"] == []

    j.evaluate_all(pid)
    j.decide(pid, "reject", significance=None)
    # Decided: the round is closed and everybody may read it.
    assert len(paper(client, pid)["reviews"]) == 3
    assert len(paper(client, pid, j.author)["reviews"]) == 3


def test_a_review_must_say_which_model_actually_wrote_it(client):
    j = Journal(client)
    pid = j.submit()
    p, a = j.panel(pid)[0]
    from tests.helpers import review_body
    body = review_body(p)
    body["assignment_id"] = a["assignment_id"]
    del body["model_provenance"]
    assert client.post("/api/reviews", json=body, headers=p.headers).status_code == 422

    body["model_provenance"] = {"provider": "Anthropic", "model_family": " CLAUDE ", "model_version": "a different model than declared"}
    assert client.post("/api/reviews", json=body, headers=p.headers).status_code == 201
    # recorded per act, and families are mapped onto the controlled vocabulary
    from sqlalchemy import select
    from app.db import SessionLocal
    from app.models import Review
    with SessionLocal() as s:
        row = s.execute(select(Review)).scalars().first()
        assert row.model_family == "Claude"


def test_nobody_can_act_on_somebody_elses_assignment(client):
    j = Journal(client)
    pid = j.submit()
    (p1, a1), (p2, _), _ = j.panel(pid)
    from tests.helpers import review_body
    body = review_body(p2)
    body["assignment_id"] = a1["assignment_id"]
    assert client.post("/api/reviews", json=body, headers=p2.headers).status_code == 404


def test_a_copied_review_is_filed_and_then_shows_up_as_inadequate_on_its_operators_record(client):
    """Bouncing it would hide the behaviour the editor exists to catch."""
    from tests.helpers import grant

    j = Journal(client, reviewers=3)
    first = j.submit("A first paper about how institutions allocate scarce reviewing attention")
    grant(j.author, 5)
    second = j.submit("A second paper about how institutions allocate scarce reviewing attention")
    on = lambda p, pid: any(a["paper_id"] == pid for a in assignments(client, p))  # noqa: E731
    spammer = next(p for p in j.everyone if on(p, first) and on(p, second))
    body = review_body(spammer, seed="same text")
    for pid in (first, second):
        a = next(a for a in assignments(client, spammer) if a["paper_id"] == pid)
        r = client.post("/api/reviews", json={**body, "assignment_id": a["assignment_id"]}, headers=spammer.headers)
        assert r.status_code == 201, r.text
    for pid in (first, second):
        j.review_all(pid)  # everyone else files their own
    # The editor marks the copy on the second paper inadequate.
    detail = client.get(f"/api/editor/manuscripts/{second}", headers=j.editor_of(second).headers).json()
    for r in detail["reviews"]:
        verdict = "inadequate" if r["reviewer"]["operator_id"] == spammer.operator_id else "adequate"
        client.post(f"/api/editor/reviews/{r['review_id']}/evaluation", json={"evaluation": verdict, "note": "copied from another paper"}, headers=j.editor_of(second).headers)
    record = client.get(f"/api/operators/{spammer.operator_id}").json()
    assert record["review_activity"]["inadequate"] == 1, "the bad review is on the record, not bounced"
