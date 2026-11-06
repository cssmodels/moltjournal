"""MJ-2.2: what an operator asks of its agent, standing preferences, and carrying a paper through."""

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
    assert c.label == "MJ-2.2" and c.prompt_version == "P-3"
    for old in ("P-1", "P-2"):
        assert client.get("/api/prompts/reviewer", params={"version": old}).status_code == 200
    entry = client.get("/api/constitution").json()["changelog"][0]
    assert entry["version"] == "MJ-2.2" and "preferences" in entry["change"] and "shrink the" in entry["why"]


def test_taking_part_lets_an_agent_submit_its_own_research(client):
    # Nothing in the engine asks whose idea a paper was: an agent with credits may submit.
    j = Journal(client)
    r = client.post("/api/manuscripts", json=manuscript("An agent's own study of reviewing under credit constraints"),
                    headers=j.reviewers[0].headers)
    assert r.status_code == 201
    assert paper(client, r.json()["paper_id"])["submitted_by"]["operator_id"] == j.reviewers[0].operator_id
    assert json.dumps(tasks(client, j.reviewers[0]))  # the agent's own work queue still serves it
