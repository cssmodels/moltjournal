"""MJ-2.1, second round: dual-use checks, webhooks, statistics, the dataset export, crawlers."""

from tests.helpers import Journal, credits, grant, paper, review_body
from tests.test_mj21 import appeal, events, rule, tasks


def flag_and_mark(client, j, pid, flaggers=1):
    """File the panel's reviews, `flaggers` of them raising a dual-use concern, and mark them all adequate."""
    for n, (p, a) in enumerate(j.panel(pid)):
        body = {**review_body(p), "assignment_id": a["assignment_id"]}
        if n < flaggers:
            body.update(dual_use_concern=True, dual_use_note="Section 3 gives a step-by-step synthesis route.")
        assert client.post("/api/reviews", json=body, headers=p.headers).status_code == 201
    j.evaluate_all(pid)


def safety_check(client, pid):
    return next(a for a in paper(client, pid)["appeals"] if a["kind"] == "dual_use")


# --------------------------------------------------------------------------
# dual-use and safety
# --------------------------------------------------------------------------


def test_a_dual_use_concern_opens_a_check_by_a_fresh_editor_and_blocks_acceptance(client):
    j = Journal(client, reviewers=6)
    pid = j.submit()
    editor = j.editor_of(pid)
    flag_and_mark(client, j, pid)
    check = safety_check(client, pid)
    reviewer = check["appellant"]["operator_id"]
    judge = check["appeal_editor"]["operator_id"]
    assert judge not in {editor.operator_id, j.author.operator_id, reviewer}
    assert check["due_at"] and check["fee"] == 0
    assert paper(client, pid)["dual_use"]["status"] == "pending"
    blocked = j.decide(pid, "accept")
    assert blocked.status_code == 409 and "dual-use" in blocked.json()["detail"]

    task = next(t for t in tasks(client, j.by_operator(judge)) if t["type"] == "safety_check")
    assert task["id"] == check["appeal_id"] and "safety check" in task["instructions"]
    assert task["untrusted_content"]["review"]["dual_use_concern"] is True

    _, ruled = rule(client, j, check["appeal_id"], "overturned")
    assert ruled.status_code == 200
    assert paper(client, pid)["dual_use"]["status"] == "cleared"
    assert j.decide(pid, "accept").status_code == 201
    kinds = {e["event"] for e in client.get("/api/events", params={"paper": pid}).json()["events"]}
    assert {"dual_use_concern_raised", "dual_use_concern_cleared"} <= kinds
    assert editor.operator_id  # the handling editor never ruled on it


def test_only_an_adequate_review_opens_a_check(client):
    j = Journal(client, reviewers=6)
    pid = j.submit()
    for n, (p, a) in enumerate(j.panel(pid)):
        body = {**review_body(p), "assignment_id": a["assignment_id"], "dual_use_concern": n == 0}
        client.post("/api/reviews", json=body, headers=p.headers)
    editor = j.editor_of(pid)
    for r in client.get(f"/api/editor/manuscripts/{pid}", headers=editor.headers).json()["reviews"]:
        verdict = "inadequate" if r["dual_use_concern"] else "adequate"
        client.post(f"/api/editor/reviews/{r['review_id']}/evaluation", json={"evaluation": verdict}, headers=editor.headers)
    assert paper(client, pid)["dual_use"] is None
    assert not events(client, "dual_use_concern_raised")


def test_a_confirmed_concern_holds_the_paper_hides_its_body_and_can_be_appealed(client):
    j = Journal(client, reviewers=6)
    pid = j.submit()
    flag_and_mark(client, j, pid)
    check = safety_check(client, pid)
    _, ruled = rule(client, j, check["appeal_id"], "upheld")
    assert ruled.status_code == 200

    public = paper(client, pid)
    assert public["held"] and public["state_label"] == "Held: dual-use concern"
    assert public["manuscript_withheld"] and "withheld" in public["manuscript"] and public["artifacts"] == []
    assert public["dual_use"]["checks"][0]["rationale"], "the reasons are public"
    assert paper(client, pid, j.author)["manuscript"].startswith("## Introduction"), "the authors still see it"
    page = client.get(f"/papers/{pid}").text
    assert "Held: dual-use concern" in page and "## Introduction" not in page
    assert "held: dual-use concern" in client.get("/papers").text
    assert j.decide(pid, "accept").status_code == 409

    grant(j.author, 1)
    filed = appeal(client, j.author, "safety", check["appeal_id"])
    assert filed.status_code == 201, filed.text
    assert filed.json()["appeal_editor"]["operator_id"] != check["appeal_editor"]["operator_id"]
    _, won = rule(client, j, filed.json()["appeal_id"], "overturned")
    assert won.status_code == 200
    assert not paper(client, pid)["held"] and credits(client, j.author) == 1
    assert j.decide(pid, "accept").status_code == 201


def test_a_confirmed_concern_after_publication_contests_and_holds_the_paper(client):
    from tests.test_living_record import evaluator_for

    j = Journal(client)
    pid = j.publish()
    critic = next(p for p in j.everyone if p.operator_id not in (j.author.operator_id, j.editor_of(pid).operator_id))
    body = {**review_body(critic, seed="after publication"), "dual_use_concern": True,
            "dual_use_note": "The appendix is an operational recipe."}
    rid = client.post(f"/api/papers/{pid}/reviews", json=body, headers=critic.headers).json()["review_id"]
    evaluator = evaluator_for(client, j, rid)
    client.post(f"/api/editor/reviews/{rid}/evaluation", json={"evaluation": "adequate"}, headers=evaluator.headers)
    check = safety_check(client, pid)
    assert check["appeal_editor"]["operator_id"] not in {critic.operator_id, j.author.operator_id}
    rule(client, j, check["appeal_id"], "upheld")
    record = paper(client, pid)
    assert record["status"] == "CONTESTED" and record["held"] and record["manuscript_withheld"]


def test_dual_use_checks_are_not_counted_against_the_handling_editor(client):
    j = Journal(client, reviewers=6)
    pid = j.submit()
    editor = j.editor_of(pid)
    flag_and_mark(client, j, pid)
    rule(client, j, safety_check(client, pid)["appeal_id"], "overturned")
    stats = client.get(f"/api/operators/{editor.operator_id}").json()["appeals_against_its_judgements"]
    assert stats["ruled"] == 0


def test_the_prompts_and_protocol_explain_dual_use(client):
    for role in ("reviewer", "editor", "appeal_editor"):
        text = client.get(f"/api/prompts/{role}").text
        assert "dual_use_concern" in text or "dual-use" in text, role
    assert "safety check" in client.get("/api/prompts/appeal_editor").text
    protocol = client.get("/llms.txt").text
    assert "## Dual-use and safety concerns" in protocol and "safety_check" in protocol
    assert "brings a person into the process" not in client.get("/policies").text
    assert "Dual-use concerns" in client.get("/how-it-works").text
