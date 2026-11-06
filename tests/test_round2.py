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


# --------------------------------------------------------------------------
# statistics
# --------------------------------------------------------------------------


def test_the_statistics_page_reads_well_when_empty(client):
    html = client.get("/statistics").text
    assert "no data yet" in html.lower() and "No appeals or safety checks yet" in html
    assert "/statistics" in client.get("/").text, "linked from the footer"
    assert "/statistics" in client.get("/about").text


def test_statistics_carry_the_papers_outcome_measures(client):
    j = Journal(client)
    pid = j.publish()
    j.submit  # noqa: B018
    s = client.get("/api/statistics").json()
    assert s["reviews"]["adequacy_rate"] == 1.0 and s["reviews"]["by_model_family"]
    assert s["panels"]["complete"] == 1 and s["panels"]["share_all_distinct"] == 1.0
    assert sum(c["pairs"] for c in s["panels"]["agreement_by_family_pair"].values()) == 3
    assert s["decisions"]["total"] == 1 and s["decisions"]["by_editor_rule"] == {"cold_start": 1}
    assert s["decisions"]["share_by_top_3_editors"] == 1.0
    for key in ("appeals", "safety_checks", "lapses", "suspensions", "living_record", "credits"):
        assert key in s
    client.post(f"/api/manuscripts/{pid}/withdraw", json={"note": "an error"}, headers=j.author.headers)
    assert client.get("/api/statistics").json()["living_record"]["status_changes"] == {"WITHDRAWN": 1}
    html = client.get("/statistics").text
    assert "Agreement between reviewers" in html and "cold start" in html


def test_the_time_to_contested_is_measured(client):
    from tests.test_living_record import evaluator_for, post_review

    j = Journal(client)
    pid = j.publish()
    critic = next(p for p in j.everyone if p.operator_id not in (j.author.operator_id, j.editor_of(pid).operator_id))
    rid = post_review(client, pid, critic, seed="timing").json()["review_id"]
    client.post(f"/api/editor/reviews/{rid}/evaluation", json={"evaluation": "adequate", "contests_paper": True},
                headers=evaluator_for(client, j, rid).headers)
    assert client.get("/api/statistics").json()["living_record"]["median_hours_to_contested"] is not None


# --------------------------------------------------------------------------
# the dataset
# --------------------------------------------------------------------------


def test_the_event_log_exports_as_jsonl_and_csv_without_private_fields(client):
    import csv
    import io
    import json

    j = Journal(client)
    j.publish()
    lines = client.get("/api/export/events.jsonl").text.strip().splitlines()
    rows = [json.loads(line) for line in lines]
    assert len(rows) == client.get("/api/statistics").json()["events"]
    assert rows[0]["seq"] == 1 and all(r["constitution_version"] == "MJ-2.2" and r["prompt_version"] == "P-3" for r in rows)
    text = client.get("/api/export/events.csv").text
    table = list(csv.DictReader(io.StringIO(text)))
    assert len(table) == len(rows) and set(table[0]) == {
        "seq", "at", "event", "constitution_version", "prompt_version", "paper_id", "operator_id", "agent_id",
        "subject_id", "payload"}
    for blob in (client.get("/api/export/events.jsonl").text, text):
        assert "@example.org" not in blob, "no contact address"
        assert "molt_" not in blob, "no API key"
        assert "whsec_" not in blob, "no webhook secret"


def test_the_codebook_documents_every_event_the_engine_can_write(client):
    import ast
    import pathlib

    documented = set(client.get("/api/export/codebook").json()["events"])
    logged = set()
    for f in pathlib.Path("app").rglob("*.py"):
        for node in ast.walk(ast.parse(f.read_text())):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "log"
                    and getattr(node.func.value, "id", "") == "events"):
                kind = node.args[1]
                for c in ast.walk(kind):
                    if isinstance(c, ast.Constant) and isinstance(c.value, str):
                        logged.add(c.value)
                if isinstance(kind, ast.JoinedStr):  # review_marked_{evaluation}
                    logged |= {"review_marked_adequate", "review_marked_inadequate"}
    logged.discard("review_marked_")
    assert logged <= documented, logged - documented


# --------------------------------------------------------------------------
# webhooks
# --------------------------------------------------------------------------


def test_a_webhook_hears_about_new_work_with_a_valid_signature(client, monkeypatch):
    import json

    from app.services import notify

    received = []
    monkeypatch.setattr(notify, "SYNC", True)
    monkeypatch.setattr(notify, "_post", lambda url, body, signature: received.append((url, body, signature)) or 200)

    j = Journal(client)
    hooked = []
    for p in j.everyone:
        r = client.patch("/api/profile", json={"webhook_url": f"https://hooks.example.org/{p.name}"}, headers=p.headers)
        assert r.status_code == 200 and r.json()["webhook_secret"].startswith("whsec_")
        hooked.append((p, r.json()["webhook_secret"]))
    assert "webhook_secret" not in client.get("/api/profile", headers=j.author.headers).json(), "shown once"
    pid = j.submit()
    reviews_ = [json.loads(b) for _, b, _ in received if json.loads(b)["type"] == "review"]
    assert len(reviews_) == 3 and all(set(b) == {"type", "task_id", "due_at"} and b["due_at"] for b in reviews_)
    secrets_by_url = {f"https://hooks.example.org/{p.name}": s for p, s in hooked}
    for url, body, signature in received:
        assert signature == notify.sign(secrets_by_url[url], body)
    received.clear()
    j.review_all(pid)
    assert any(json.loads(b)["type"] == "editor" for _, b, _ in received), "the editor is told when the panel is complete"


def test_a_failing_webhook_is_retried_then_logged_and_never_blocks_the_request(client, monkeypatch):
    from app.services import notify

    calls = []

    def broken(url, body, signature):
        calls.append(url)
        raise OSError("connection refused")

    monkeypatch.setattr(notify, "SYNC", True)
    monkeypatch.setattr(notify, "_post", broken)
    j = Journal(client)
    for p in j.everyone:
        client.patch("/api/profile", json={"webhook_url": "https://down.example.org/hook"}, headers=p.headers)
    pid = j.submit()
    assert pid and len(calls) == 3 * (notify.RETRIES + 1)
    assert len(events(client, "webhook_failed")) == 3


def test_webhook_urls_must_be_https_and_private_addresses_are_refused(client):
    from app.services import notify

    j = Journal(client)
    assert client.patch("/api/profile", json={"webhook_url": "http://example.org/x"}, headers=j.author.headers).status_code == 422
    assert not notify._public("127.0.0.1") and not notify._public("10.0.0.5") and not notify._public("localhost")
    assert client.patch("/api/profile", json={"webhook_url": ""}, headers=j.author.headers).json()["webhook_url"] is None
    assert "webhook_url" in client.get("/llms.txt").text and "X-Molt-Signature" in client.get("/llms.txt").text


# --------------------------------------------------------------------------
# consent, privacy and removal
# --------------------------------------------------------------------------


def test_registration_needs_consent_and_logs_its_version(client):
    body = {"operator": {"name": "Unsure Lab", "type": "lab", "contact": "unsure@example.org"},
            "agent": {"name": "u", "model_provenance": {"provider": "P", "model_family": "Claude", "model_version": "v"}}}
    assert client.post("/api/accounts", json=body).status_code == 422
    refused = client.post("/api/accounts", json={**body, "accepts_terms": False})
    assert refused.status_code == 422 and "CC BY 4.0" in refused.text
    assert client.post("/api/accounts", json={**body, "accepts_terms": True}).status_code == 201
    assert events(client, "operator_registered")[0]["payload"]["consent_version"] == "T-1"
    assert "accepts_terms" in client.get("/llms.txt").text
    assert "accepts_terms" in client.get("/openapi.json").text


def test_policies_state_privacy_ethics_and_no_warranty_without_inventing_a_review(client):
    text = client.get("/policies").text
    for must in ("What is published", "What stays private", "Retention", "Removal", "pseudonym",
                 "has not undergone institutional ethics review", "data controller", "without warranty",
                 "p.tornberg@uva.nl"):
        assert must in text, must
    assert "PLACEHOLDER" not in text and "approved by" not in text.lower()


def test_a_pseudonymised_operator_disappears_from_the_published_record(client):
    import json

    j = Journal(client)
    pid = j.publish()
    who = j.author
    from tests.helpers import admin
    assert admin(client, "POST", f"/api/admin/operators/{who.operator_id}/pseudonymise").status_code == 200
    record = client.get(f"/api/operators/{who.operator_id}").json()
    assert record["name"].startswith("Pseudonymised operator") and record["affiliation"] is None
    assert paper(client, pid)["state"] == "PUBLISHED", "the scientific record stays"
    for blob in (json.dumps(client.get("/api/events", params={"limit": 1000}).json()),
                 client.get("/api/export/events.jsonl").text):
        assert "Author Lab" not in blob
    assert events(client, "operator_pseudonymised")


def test_the_site_is_developed_by_petter_tornberg_not_branded_as_the_university(client):
    home = client.get("/").text
    assert "Developed by Petter Törnberg" in home and "A research project at the" not in home
    assert "uva.nl/en/profile" not in home and "pettertornberg.com" in home and "scholar.google" in home
    assert "started at the University of Amsterdam" in client.get("/about").text
    assert "University of Amsterdam" not in home


# --------------------------------------------------------------------------
# crawlers, previews and the rate limit
# --------------------------------------------------------------------------


def test_search_and_preview_crawlers_get_the_human_page(client):
    j = Journal(client)
    pid = j.publish()
    for ua in ("Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)", "Twitterbot/1.0",
               "facebookexternalhit/1.1 (+http://www.facebook.com/externalhit_uatext.php)",
               "Slackbot-LinkExpanding 1.0 (+https://api.slack.com/robots)"):
        for path in ("/", f"/papers/{pid}"):
            r = client.get(path, headers={"User-Agent": ua, "Accept": "*/*"})
            assert r.headers["content-type"].startswith("text/html"), (ua, path)
            assert 'property="og:image" content="http' in r.text
    assert client.get("/", headers={"User-Agent": "python-httpx/0.27", "Accept": "*/*"}).text.startswith("# MoltJournal")


def test_og_image_is_absolute_and_https_in_production(client, monkeypatch):
    from app import config

    monkeypatch.setattr(config, "ENVIRONMENT", "production")
    html = client.get("/").text
    assert '<meta property="og:image" content="https://testserver/static/hero.png">' in html


def test_each_worker_enforces_its_share_of_the_rate_limit(monkeypatch):
    from app import config

    monkeypatch.setattr(config, "WORKERS", 2)
    assert config.per_worker_limit(120) == 60 and config.per_worker_limit(5) == 3 and config.per_worker_limit(1) == 1
