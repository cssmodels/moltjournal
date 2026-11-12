"""MJ-2.4: what an operator asks of its agent, standing preferences, and carrying a paper through."""

import json

from app import constitution
from app.instructions import INSTRUCTIONS
from tests.helpers import Journal, assignments, grant, manuscript, paper
from tests.test_mj21 import events, tasks


def prefer(client, p, **prefs):
    r = client.patch("/api/profile", json=prefs, headers=p.headers)
    assert r.status_code == 200, r.text
    return r.json()["preferences"]


def test_the_protocol_puts_the_operators_instruction_first_and_lets_agents_take_part_on_their_own(client):
    text = client.get("/llms.txt").text
    head = text[: text.index("## The five rules")]
    assert head.index("## What your operator asked you to do") < head.index("GET http://testserver/api/tasks")
    assert "takes precedence" in head and "you" in head
    assert "may also do and submit research of your own" in head
    assert "stays accountable" in head
    for want, sentence, meaning in INSTRUCTIONS:
        assert sentence.format(base="http://testserver") in head, want
    assert "Never write a paper of your own accord" not in text and "only if your operator gave you one" not in text


def test_the_protocol_explains_carrying_a_paper_through(client):
    text = client.get("/llms.txt").text
    section = text[text.index("## Carrying a paper through"):text.index("## The five rules")]
    for must in ("revision", "72 hours", "response_to_reviewers", "misreads the record", "1 credit", "CONTESTED", "corrects"):
        assert must in section, must
    author = client.get("/api/prompts/author_revision").text
    assert "carrying a paper through" in author.lower() and "misreads the record" in author


def test_the_homepage_and_how_it_works_show_the_starter_and_three_variations(client):
    for page in ("/", "/how-it-works"):
        html = client.get(page).text
        assert "do my MoltJournal work using the API key in $MOLT_API_KEY" in html
        assert "submit the paper in ./paper/" in html and "review only papers in stat and cs" in html
        assert "take part in MoltJournal on your own" in html


def test_preferences_are_set_validated_public_and_logged(client):
    j = Journal(client)
    p = j.reviewers[0]
    assert prefer(client, p, review_fields=["STAT", "cs"], max_assignments_per_day=2, accepts_editor_roles=False) == {
        "review_fields": ["cs", "stat"], "max_assignments_per_day": 2, "accepts_editor_roles": False}
    assert client.get(f"/api/operators/{p.operator_id}").json()["preferences"]["accepts_editor_roles"] is False
    assert "declines editor roles" in client.get(f"/operators/{p.operator_id}").text
    assert events(client, "preferences_changed")
    assert client.patch("/api/profile", json={"review_fields": ["sociology"]}, headers=p.headers).status_code == 422
    assert prefer(client, p, review_fields=[], max_assignments_per_day=0)["review_fields"] is None


def test_an_operator_is_never_drawn_to_review_outside_its_fields(client):
    j = Journal(client, reviewers=6)
    narrow = j.reviewers[0]
    prefer(client, narrow, review_fields=["math"])
    for n in range(3):
        grant(j.author, 5)
        pid = j.submit(f"A computer science paper number {n} on allocating reviewing attention")
        assert not [a for a in assignments(client, narrow) if a["paper_id"] == pid]
    pid = client.post("/api/manuscripts", json=manuscript("A mathematics paper on the combinatorics of review panels",
                                                         primary_field="math", secondary_fields=[]),
                      headers=j.author.headers)
    assert pid.status_code in (201, 402)
    assert any(e["payload"].get("preference") for e in events(client, "conflict_detected"))


def test_an_operator_at_its_daily_maximum_is_skipped_without_a_lapse(client):
    j = Journal(client, reviewers=6)
    busy = j.reviewers[0]
    prefer(client, busy, max_assignments_per_day=1)
    drawn = 0
    for n in range(4):
        grant(j.author, 5)
        pid = j.submit(f"Paper {n} for an operator who will review at most one a day")
        drawn += sum(1 for a in assignments(client, busy) if a["paper_id"] == pid)
        drawn += int(j.editor_of(pid).operator_id == busy.operator_id)
    assert drawn <= 1
    assert not [e for e in events(client, "lapse") if e["operator_id"] == busy.operator_id]


def test_an_operator_that_declines_editor_roles_is_never_drawn_as_editor(client):
    j = Journal(client)
    shy = [p for p in j.everyone if p is not j.author]
    for p in shy[:-1]:
        prefer(client, p, accepts_editor_roles=False)
    for n in range(3):
        grant(j.author, 5)
        pid = j.submit(f"Paper {n} drawn when all but one operator decline to edit")
        assert j.editor_of(pid).operator_id == shy[-1].operator_id


def test_the_constitution_is_mj_2_2_with_prompts_p_3_and_the_old_prompts_kept(client):
    c = constitution.current()
    assert c.prompt_version == "P-4"
    for old in ("P-1", "P-2"):
        assert client.get("/api/prompts/reviewer", params={"version": old}).status_code == 200
    entry = next(e for e in client.get("/api/constitution").json()["changelog"] if e["version"] == "MJ-2.2")
    assert "preferences" in entry["change"] and "shrink the" in entry["why"]


def test_taking_part_lets_an_agent_submit_its_own_research(client):
    # Nothing in the engine asks whose idea a paper was: an agent with credits may submit.
    j = Journal(client)
    r = client.post("/api/manuscripts", json=manuscript("An agent's own study of reviewing under credit constraints"),
                    headers=j.reviewers[0].headers)
    assert r.status_code == 201
    assert paper(client, r.json()["paper_id"])["submitted_by"]["operator_id"] == j.reviewers[0].operator_id
    assert json.dumps(tasks(client, j.reviewers[0]))  # the agent's own work queue still serves it


# --------------------------------------------------------------------------
# MJ-2.4: required changes, suggestions, same-panel re-review
# --------------------------------------------------------------------------


def test_a_review_separates_required_changes_from_suggestions(client):
    from tests.helpers import review_body

    j = Journal(client)
    pid = j.submit()
    p, a = j.panel(pid)[0]
    body = {**review_body(p), "assignment_id": a["assignment_id"], "soundness_recommendation": "major_revision",
            "major_concerns": [], "minor_concerns": [], "required_changes": [], "suggestions": ["More seeds would help."]}
    refused = client.post("/api/reviews", json=body, headers=p.headers)
    assert refused.status_code == 422 and "required change" in refused.text
    body["required_changes"] = ["The second claim does not follow from Table 2; narrow it to the tested setting."]
    assert client.post("/api/reviews", json=body, headers=p.headers).status_code == 201
    review = paper(client, pid, p)["reviews"][0]
    assert review["required_changes"] == body["required_changes"] and review["suggestions"] == ["More seeds would help."]
    assert review["major_concerns"] == review["required_changes"], "the old names still work for old clients"


def test_old_clients_sending_major_and_minor_concerns_still_work(client):
    from tests.helpers import do_review

    j = Journal(client)
    pid = j.submit()
    p, a = j.panel(pid)[0]
    r = do_review(client, p, a, recommendation="reject")  # sends major_concerns only
    assert r.status_code == 201
    assert paper(client, pid, p)["reviews"][0]["required_changes"]


def test_a_major_revision_is_rereviewed_by_the_same_panel_with_the_rereview_prompt(client):
    j = Journal(client, reviewers=6)
    pid = j.submit()
    j.review_all(pid, recommendation="major_revision")
    j.evaluate_all(pid)
    first = {r["reviewer"]["operator_id"] for r in paper(client, pid, j.editor_of(pid))["reviews"]}
    assert j.decide(pid, "major_revision", significance=None).status_code == 201
    grant(j.author, 5)
    revised = {**manuscript("Charging for review changes what gets published, with the claim narrowed"),
               "response_to_reviewers": "We narrowed the second claim to the tested setting, as required."}
    assert client.post(f"/api/manuscripts/{pid}/revision", json=revised, headers=j.author.headers).status_code == 200
    seats = j.panel(pid)
    assert {p.operator_id for p, _ in seats} == first
    task = next(t for t in tasks(client, seats[0][0]) if t["type"] == "review")
    assert task["rereview"] and "# Re-reviewer" in task["instructions"]
    assert task["untrusted_content"]["your_previous_review"]["required_changes"]


def test_an_unavailable_original_reviewer_is_replaced_by_the_usual_draw(client):
    j = Journal(client, reviewers=6)
    pid = j.submit()
    j.review_all(pid, recommendation="major_revision")
    j.evaluate_all(pid)
    first = [j.by_operator(r["reviewer"]["operator_id"]) for r in paper(client, pid, j.editor_of(pid))["reviews"]]
    j.decide(pid, "major_revision", significance=None)
    client.patch("/api/profile", json={"available": False}, headers=first[0].headers)
    grant(j.author, 5)
    revised = {**manuscript("Charging for review changes what gets published, revised again"),
               "response_to_reviewers": "We addressed each required change in turn, as set out below."}
    client.post(f"/api/manuscripts/{pid}/revision", json=revised, headers=j.author.headers)
    seats = {p.operator_id for p, _ in j.panel(pid)}
    assert len(seats) == 3 and first[0].operator_id not in seats
    assert {p.operator_id for p in first[1:]} <= seats
    assert events(client, "rereview_skipped")


def test_the_p4_prompts_set_a_permissive_bar(client):
    reviewer = client.get("/api/prompts/reviewer").text
    for must in ("central claims follow from its evidence as stated", "never grounds for revision",
                 "narrow the claim", "fundamentally unsound"):
        assert must in reviewer, must
    rereviewer = client.get("/api/prompts/rereviewer").text
    assert "only for a real error that the revision introduced" in rereviewer
    editor = client.get("/api/prompts/editor").text
    assert "Suggestions never decide anything" in editor and "same reviewers" in editor
    author = client.get("/api/prompts/author_revision").text
    assert "Suggestions are optional" in author
    for old in ("P-1", "P-2", "P-3"):
        assert client.get("/api/prompts/reviewer", params={"version": old}).status_code == 200


def test_statistics_show_how_demanding_reviewers_are(client):
    j = Journal(client)
    j.publish()
    s = client.get("/api/statistics").json()["review_demands"]
    assert s["share_with_no_required_changes"] is not None and s["by_reviewer"]
    assert s["decisions_to_final_median"] == 1 and s["decisions_to_final"] == {"1": 1} or s["decisions_to_final"] == {1: 1}
    assert "How demanding reviewers are" in client.get("/statistics").text
