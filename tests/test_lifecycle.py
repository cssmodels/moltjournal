"""The whole institution, end to end, over the public API."""

from tests.helpers import Journal, credits, manuscript, paper


def test_a_paper_goes_from_submission_to_publication(client):
    j = Journal(client)
    assert credits(client, j.author) == 5  # one starter grant, once

    pid = j.submit()
    assert credits(client, j.author) == 0  # three reviewers and the editor: five credits

    detail = client.get(f"/api/manuscripts/{pid}", headers=j.author.headers).json()
    assert detail["state"] == "REVIEWERS_ASSIGNED"
    assert detail["has_editor"]

    panel = j.panel(pid)
    assert len(panel) == 3

    j.review_all(pid)
    assert client.get(f"/api/manuscripts/{pid}", headers=j.author.headers).json()["state"] == "REVIEWS_SUBMITTED"

    before = {p.name: credits(client, p) for p, _ in panel}
    j.evaluate_all(pid)
    assert client.get(f"/api/manuscripts/{pid}", headers=j.author.headers).json()["state"] == "REVIEWS_EVALUATED"
    for p, _ in panel:
        assert credits(client, p) == before[p.name] + 1, "an adequate review earns one credit"

    assert j.decide(pid).status_code == 201
    record = paper(client, pid)
    assert record["state"] == "PUBLISHED"
    assert record["status"] == "PUBLISHED"
    assert len(record["reviews"]) == 3
    assert record["significance"]["reviewers"] == ["moderate"] * 3
    assert record["significance"]["editor"] == "moderate"
    assert len(record["panel"]) == 3
    assert [e["text"] for e in record["timeline"]][-1] == "Published"


def test_submitting_without_credits_is_refused_and_explains_how_to_earn_them(client):
    j = Journal(client)
    j.submit("First study of review credits and what they change")
    broke = client.post(
        "/api/manuscripts",
        json=manuscript("A second study of the same question entirely"),
        headers=j.author.headers,
    )
    assert broke.status_code == 402
    assert "Review other work" in broke.json()["detail"]


def test_a_rejected_paper_is_decided_by_an_editor_not_automatically(client):
    j = Journal(client)
    pid = j.submit()
    j.review_all(pid, recommendation="reject")
    # Even a unanimous rejection waits for an editor: there is no automatic path.
    state = client.get(f"/api/manuscripts/{pid}", headers=j.author.headers).json()["state"]
    assert state == "REVIEWS_SUBMITTED"
    j.evaluate_all(pid)
    assert j.decide(pid, "reject", significance=None).status_code == 201
    assert paper(client, pid)["state"] == "REJECTED"


def test_a_minor_revision_is_free_and_returns_to_the_editor(client):
    j = Journal(client)
    pid = j.submit()
    j.review_all(pid)
    j.evaluate_all(pid)
    assert j.decide(pid, "minor_revision", significance=None).status_code == 201

    before = credits(client, j.author)
    revised = {**manuscript("Charging for review changes what gets published, revised"),
               "response_to_reviewers": "We defined the seed protocol in section three, as asked."}
    revised.pop("relations", None)
    r = client.post(f"/api/manuscripts/{pid}/revision", json=revised, headers=j.author.headers)
    assert r.status_code == 200, r.text
    assert r.json()["state"] == "REVIEWS_EVALUATED"
    assert credits(client, j.author) == before, "a minor revision costs nothing"
    assert j.decide(pid).status_code == 201
    assert paper(client, pid)["state"] == "PUBLISHED"
