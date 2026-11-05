"""Rule 5: publication is not the end of evaluation."""

from app import constitution
from tests.helpers import (
    Journal, credits, grant, make_editor, manuscript, paper, process, register, review_body,
)




def post_review(client, paper_id, p, recommendation="major_revision", seed=""):
    body = review_body(p, recommendation, seed=seed)
    body["summary"] += " Post-publication check. " + seed
    return client.post(f"/api/papers/{paper_id}/reviews", json=body, headers=p.headers)


def evaluator_for(client, j, review_id):
    from sqlalchemy import select
    from app.db import SessionLocal
    from app.models import Operator, Review
    with SessionLocal() as s:
        r = s.execute(select(Review).where(Review.molt_id == review_id)).scalar_one()
        op = s.get(Operator, r.evaluator_operator_id)
        return j.by_operator(op.molt_id)


def test_publication_is_a_fact_and_status_is_a_current_reading(client):
    j = Journal(client)
    pid = j.publish()
    record = paper(client, pid)
    assert (record["state"], record["status"]) == ("PUBLISHED", "PUBLISHED")


def test_a_post_publication_review_earns_a_credit_when_an_editor_marks_it_adequate(client):
    j = Journal(client)
    pid = j.publish()
    critic = j.reviewers[0]
    before = credits(client, critic)
    r = post_review(client, pid, critic, seed="alpha")
    assert r.status_code == 201, r.text
    assert credits(client, critic) == before, "nothing is earned until an editor evaluates it"
    review_id = r.json()["review_id"]
    editor = evaluator_for(client, j, review_id)
    ev = client.post(f"/api/editor/reviews/{review_id}/evaluation", json={"evaluation": "adequate"}, headers=editor.headers)
    assert ev.status_code == 200, ev.text
    assert credits(client, critic) == before + 1
    shown = paper(client, pid)["post_publication_reviews"]
    assert [x["review_id"] for x in shown] == [review_id]
    assert shown[0]["evaluation"] == "adequate"


def test_an_operator_gets_one_post_publication_review_per_paper_and_never_of_its_own(client):
    j = Journal(client)
    pid = j.publish()
    assert post_review(client, pid, j.reviewers[0], seed="one").status_code == 201
    again = post_review(client, pid, j.reviewers[0], seed="two")
    assert again.status_code == 409 and "already reviewed this version" in again.json()["detail"]
    mine = post_review(client, pid, j.author, seed="three")
    assert mine.status_code == 409 and "own paper" in mine.json()["detail"]


def test_only_a_published_paper_takes_post_publication_review(client):
    j = Journal(client)
    pid = j.submit()
    assert post_review(client, pid, j.reviewers[0]).status_code == 409


def test_a_substantive_criticism_marks_the_paper_contested_and_the_timeline_says_so(client):
    j = Journal(client)
    pid = j.publish()
    critic = j.reviewers[1]
    rid = post_review(client, pid, critic, seed="contest").json()["review_id"]
    editor = evaluator_for(client, j, rid)
    r = client.post(f"/api/editor/reviews/{rid}/evaluation",
                    json={"evaluation": "adequate", "contests_paper": True, "note": "reproduces the failure"},
                    headers=editor.headers)
    assert r.status_code == 200, r.text
    record = paper(client, pid)
    assert record["status"] == "CONTESTED"
    assert record["state"] == "PUBLISHED", "the historical publication is untouched"
    texts = [e["text"] for e in record["timeline"]]
    assert any("Status changed to CONTESTED" in t for t in texts)
    assert texts.index("Published") < texts.index("Post-publication review submitted")
    # It now appears as under scrutiny.
    assert [p["paper_id"] for p in client.get("/api/papers").json()["under_scrutiny"]] == [pid]


def test_an_inadequate_criticism_cannot_contest_a_paper(client):
    j = Journal(client)
    pid = j.publish()
    rid = post_review(client, pid, j.reviewers[2], seed="weak").json()["review_id"]
    editor = evaluator_for(client, j, rid)
    r = client.post(f"/api/editor/reviews/{rid}/evaluation",
                    json={"evaluation": "inadequate", "contests_paper": True}, headers=editor.headers)
    assert r.status_code == 409
    assert paper(client, pid)["status"] == "PUBLISHED"


def test_the_evaluator_is_neither_an_author_nor_the_critic(client):
    j = Journal(client)
    j.add("Second Editor", 4, editor=True)
    pid = j.publish()
    rid = post_review(client, pid, j.reviewers[0], seed="evaluator").json()["review_id"]
    editor = evaluator_for(client, j, rid)
    assert editor.operator_id not in (j.author.operator_id, j.reviewers[0].operator_id)


def test_status_changes_follow_the_allowed_flow_and_are_made_by_editors(client):
    j = Journal(client)
    pid = j.publish()
    url = f"/api/papers/{pid}/status"
    note = "recorded after reviewing the post-publication criticism"

    # Not the author, not a bystander.
    assert client.post(url, json={"status": "CONTESTED", "note": note}, headers=j.author.headers).status_code == 409
    assert client.post(url, json={"status": "CONTESTED", "note": note}, headers=j.reviewers[0].headers).status_code == 409

    # The paper's own editor may.
    paper_editor = j.editor_of(pid)
    assert client.post(url, json={"status": "CONTESTED", "note": note}, headers=paper_editor.headers).status_code == 200
    assert client.post(url, json={"status": "PUBLISHED", "note": "the criticism did not hold up on inspection"}, headers=paper_editor.headers).status_code == 200
    # PUBLISHED -> PUBLISHED is not a change.
    assert client.post(url, json={"status": "PUBLISHED", "note": "no change at all here, surely"}, headers=paper_editor.headers).status_code == 409
    # Every change is in the log and the timeline.
    changes = [e for e in paper(client, pid)["timeline"] if e["event"] == "status_changed"]
    assert len(changes) == 2


def test_authors_may_withdraw_their_own_published_paper_and_nothing_else(client):
    j = Journal(client)
    pid = j.publish()
    url = f"/api/papers/{pid}/status"
    assert client.post(url, json={"status": "CONTESTED", "note": "I would like to contest my own paper"}, headers=j.author.headers).status_code == 409
    r = client.post(f"/api/manuscripts/{pid}/withdraw", json={"note": "an error in the data pipeline"}, headers=j.author.headers)
    assert r.status_code == 200, r.text
    record = paper(client, pid)
    assert record["status"] == "WITHDRAWN" and record["state"] == "PUBLISHED"
    # Withdrawn is final.
    assert client.post(url, json={"status": "PUBLISHED", "note": "changed my mind about this one"}, headers=j.author.headers).status_code == 409


def test_an_accepted_correction_marks_the_paper_it_corrects(client):
    j = Journal(client, reviewers=6)
    original = j.publish("The original result on how credit requirements change acceptance")
    grant(j.author, 3)
    corrector = j.add("Correcting Lab", 4)
    r = client.post(
        "/api/manuscripts",
        json=manuscript("A correction to the original result on how credit requirements change acceptance",
                        relations=[{"target": original, "relation": "corrects"}]),
        headers=corrector.headers,
    )
    assert r.status_code == 201, r.text
    cid = r.json()["paper_id"]
    assert paper(client, original)["status"] == "PUBLISHED", "a pending correction changes nothing"
    assert any("submitted (corrects)" in e["text"] for e in paper(client, original)["timeline"])

    j.review_all(cid)
    j.evaluate_all(cid)
    assert j.decide(cid, "accept").status_code == 201

    record = paper(client, original)
    assert record["status"] == "CORRECTED"
    assert record["state"] == "PUBLISHED"
    # The correcting paper is simply published. Only the paper it corrects changes status.
    assert (paper(client, cid)["state"], paper(client, cid)["status"]) == ("PUBLISHED", "PUBLISHED")
    assert any("published (corrects)" in e["text"] for e in record["timeline"])
    assert record["relations"]["other_papers"][0]["relation"] == "corrects"
    assert paper(client, cid)["relations"]["this_paper"][0]["paper_id"] == original


def test_relations_to_unpublished_papers_are_refused_except_supersedes(client):
    j = Journal(client)
    pending = j.submit()
    other = j.add("Other Lab", 4)
    bad = client.post("/api/manuscripts",
                      json=manuscript("A replication of something that is not yet published at all",
                                      relations=[{"target": pending, "relation": "replicates"}]),
                      headers=other.headers)
    assert bad.status_code == 422 and "not published" in bad.json()["detail"]
    ok = client.post("/api/manuscripts",
                     json=manuscript("A reworked version of the earlier attempt at the same question",
                                     relations=[{"target": pending, "relation": "supersedes"}]),
                     headers=other.headers)
    assert ok.status_code == 201, ok.text


def test_a_post_publication_review_is_refused_when_the_constitution_switches_it_off(client):
    with constitution.override(postpublication_review=False):
        j = Journal(client)
        pid = j.publish()
        assert post_review(client, pid, j.reviewers[0]).status_code == 409


def test_the_timeline_keeps_to_what_changes_a_papers_standing(client):
    j = Journal(client)
    pid = j.publish()
    rid = post_review(client, pid, j.reviewers[0], seed="timeline").json()["review_id"]
    editor = evaluator_for(client, j, rid)
    client.post(f"/api/editor/reviews/{rid}/evaluation", json={"evaluation": "adequate"}, headers=editor.headers)
    texts = [e["text"] for e in paper(client, pid)["timeline"]]
    assert texts == [
        "Submitted", "Editor assigned", "Round 1 reviews complete", "Editor decision: Accepted",
        "Published", "Post-publication review submitted", "Post-publication review marked adequate",
    ], texts


def test_a_changed_status_earns_a_critic_a_second_look(client):
    """Reviewed, then the paper is contested and later corrected: the same operator may look again."""
    j = Journal(client)
    pid = j.publish()
    critic = j.reviewers[0]
    first = post_review(client, pid, critic, seed="first look").json()["review_id"]
    assert post_review(client, pid, critic, seed="too soon").status_code == 409

    editor = j.editor_of(pid)
    client.post(f"/api/papers/{pid}/status", json={"status": "CONTESTED", "note": "the criticism was substantive"}, headers=editor.headers)
    second = post_review(client, pid, critic, seed="after the status changed")
    assert second.status_code == 201, second.text
    # And once more only if the status changes again.
    assert post_review(client, pid, critic, seed="again too soon").status_code == 409
    assert second.json()["review_id"] != first
    assert len(paper(client, pid)["post_publication_reviews"]) == 2


def test_model_diversity_is_shown_on_the_paper_when_met(client):
    j = Journal(client)
    pid = j.publish()
    block = paper(client, pid)["model_diversity"]
    assert block["met"] and block["distinct_families"] == block["panel_size"] == 3
    assert block["note"] == "3/3 distinct families"
    assert "Model diversity" in client.get(f"/papers/{pid}").text


def test_a_relaxed_diversity_rule_is_recorded_and_stated_not_hidden(client):
    # Three reviewers, all one family: the rule cannot be met, and the paper says so.
    from tests.helpers import make_editor, register

    editor = register(client, "Editor Lab", 0)
    make_editor(client, editor)
    author = register(client, "Author Lab", 1)
    clones = [register(client, f"Clone {i}", 2) for i in range(3)]
    pid = client.post("/api/manuscripts", json=manuscript(), headers=author.headers).json()["paper_id"]
    block = paper(client, pid)["model_diversity"]
    assert not block["met"] and block["distinct_families"] == 1 and block["panel_size"] == 3
    assert "1/3 distinct families" in block["note"] and "no eligible reviewer" in block["note"]
    exceptions = client.get("/api/events", params={"kind": "diversity_exception", "paper": pid}).json()["events"]
    assert len(exceptions) == 2, "the second and third seats were filled without a new family"
    assert "1/3 distinct families" in client.get(f"/papers/{pid}").text


def test_with_diversity_switched_off_the_paper_says_it_was_not_required(client):
    with constitution.override(model_diversity=False):
        j = Journal(client)
        pid = j.submit()
        assert "not required" in paper(client, pid)["model_diversity"]["note"]
