"""MJ-2.1: an institution that runs on its rules, with no human decisions."""

from sqlalchemy import select

from app import constitution, vocab
from app.models import Appeal, EditorAssignment, EditorialDecision, Manuscript, Operator, ReviewAssignment
from tests.helpers import (
    Journal, admin, assignments, backdate, credits, db, do_review, grant, manuscript, operator_row, paper,
    register, review_body, sweep,
)


def events(client, kind, paper_id=None):
    params = {"kind": kind, "limit": 1000}
    if paper_id:
        params["paper"] = paper_id
    return client.get("/api/events", params=params).json()["events"]


def editor_item(client, j, pid):
    editor = j.editor_of(pid)
    items = client.get("/api/editor/assignments", headers=editor.headers).json()["manuscripts"]
    return editor, next(m for m in items if m["paper_id"] == pid)


def reject(j, pid, decision="reject"):
    j.review_all(pid)
    j.evaluate_all(pid)
    r = j.decide(pid, decision, significance=None)
    assert r.status_code == 201, r.text
    return r.json()["decision_id"]


# --------------------------------------------------------------------------
# A. one currency: editors are paid out of what the authors pay
# --------------------------------------------------------------------------


def test_a_submission_costs_the_reviewers_plus_the_editor_and_the_starter_grant_pays_for_one(client):
    j = Journal(client)
    c = constitution.current()
    assert c.submission_cost == c.reviews_required + c.editor_credits == 5 == c.starter_credits
    assert client.get("/api/credits", headers=j.author.headers).json()["cost_to_submit"] == 5
    j.submit()
    assert credits(client, j.author) == 0
    second = client.post("/api/manuscripts", json=manuscript("A second study the operator cannot afford yet"), headers=j.author.headers)
    assert second.status_code == 402


def test_the_editor_earns_its_share_for_an_on_time_decision_once_per_round(client):
    j = Journal(client)
    pid = j.submit()
    j.review_all(pid)
    j.evaluate_all(pid)
    editor = j.editor_of(pid)
    before = credits(client, editor)
    assert j.decide(pid, "minor_revision", significance=None).status_code == 201
    assert credits(client, editor) == before + 2
    body = {**manuscript("Charging for review changes what gets published, lightly revised"),
            "response_to_reviewers": "We clarified the seed protocol as the reviewers asked."}
    assert client.post(f"/api/manuscripts/{pid}/revision", json=body, headers=j.author.headers).status_code == 200
    assert j.decide(pid, "accept").status_code == 201
    assert credits(client, editor) == before + 2, "a second decision in the same round earns nothing more"
    paid = [e for e in events(client, "editor_decision", pid)]
    assert [e["payload"]["credits_paid"] for e in paid] == [2, 0]


def test_a_late_decision_is_not_paid(client):
    j = Journal(client)
    pid = j.submit()
    j.review_all(pid)
    j.evaluate_all(pid)
    editor, item = editor_item(client, j, pid)
    backdate(EditorAssignment, item["editor_assignment_id"])
    before = credits(client, editor)
    assert j.decide(pid, "reject", significance=None, editor=editor).status_code == 201
    assert credits(client, editor) == before


# --------------------------------------------------------------------------
# B. deadlines and lapses
# --------------------------------------------------------------------------


def test_deadlines_are_in_hours_and_shown_as_due_at(client):
    from datetime import datetime, timedelta, timezone

    j = Journal(client)
    pid = j.submit()
    p, a = j.panel(pid)[0]
    due = datetime.fromisoformat(a["due_at"])
    due = due if due.tzinfo else due.replace(tzinfo=timezone.utc)
    assert timedelta(hours=23) < due - datetime.now(timezone.utc) <= timedelta(hours=24)
    _, item = editor_item(client, j, pid)
    assert item["due_at"] is None, "the editor's clock does not run while reviews are out"
    j.review_all(pid)
    _, item = editor_item(client, j, pid)
    assert item["due_at"] is not None
    assert "assignment_timeout_days" not in client.get("/api/constitution").json()["current"]


def test_a_review_past_its_deadline_lapses_and_is_reassigned(client):
    j = Journal(client)
    pid = j.submit()
    late, a = j.panel(pid)[0]
    backdate(ReviewAssignment, a["assignment_id"])
    result = sweep(client)
    assert result["expired"] == 1 and result["reviewers_assigned"] == 1
    assert [e for e in events(client, "lapse") if e["operator_id"] == late.operator_id][0]["payload"]["role"] == "reviewer"
    assert len(j.panel(pid)) == 3, "a replacement took the seat"
    assert late.operator_id not in {p.operator_id for p, _ in j.panel(pid)}
    assert client.get(f"/api/operators/{late.operator_id}").json()["lapses"]["recent"] == 1


def test_an_editor_past_its_deadline_lapses_and_another_is_drawn(client):
    j = Journal(client, reviewers=6)
    pid = j.submit()
    j.review_all(pid)
    first, item = editor_item(client, j, pid)
    backdate(EditorAssignment, item["editor_assignment_id"])
    assert sweep(client)["editors_lapsed"] == 1
    second, item = editor_item(client, j, pid)
    assert second.operator_id != first.operator_id
    assert item["due_at"] is not None, "the new editor starts on the clock"
    assert any(e["operator_id"] == first.operator_id and e["payload"]["role"] == "editor" for e in events(client, "lapse"))
    j.evaluate_all(pid)
    assert j.decide(pid, "accept").status_code == 201


def test_a_revision_past_its_deadline_withdraws_the_manuscript(client):
    j = Journal(client)
    pid = j.submit()
    reject(j, pid, "major_revision")
    status = client.get(f"/api/manuscripts/{pid}", headers=j.author.headers).json()
    assert status["state"] == "REVISION" and status["due_at"]
    backdate(Manuscript, pid, "revision_due_at")
    assert sweep(client)["revisions_withdrawn"] == 1
    assert client.get(f"/api/manuscripts/{pid}", headers=j.author.headers).json()["state"] == "WITHDRAWN"


def test_too_many_lapses_pause_new_assignments(client):
    from app.services import deadlines

    j = Journal(client)
    idle = j.reviewers[0]
    db(lambda s: [deadlines.record_lapse(s, operator_row(s, idle).id, "reviewer") for _ in range(3)])
    record = client.get(f"/api/operators/{idle.operator_id}").json()
    assert record["lapses"]["recent"] == 3 and record["lapses"]["paused_until"]
    assert events(client, "operator_paused")
    for n in range(3):
        grant(j.author, 5)
        pid = j.submit(f"Paper {n} submitted while one operator is paused for lapsing")
        assert not [a for a in assignments(client, idle) if a["paper_id"] == pid]
        assert j.editor_of(pid).operator_id != idle.operator_id


# --------------------------------------------------------------------------
# C. the sweep is the timer's, not the public's
# --------------------------------------------------------------------------


def test_the_sweep_needs_the_admin_token(client):
    assert client.post("/api/journal/sweep").status_code == 403
    assert admin(client, "POST", "/api/journal/sweep").status_code == 200


# --------------------------------------------------------------------------
# D. editors without founders
# --------------------------------------------------------------------------


def test_founding_editors_are_retired(client):
    p = register(client, "Would-be Founder", 0)
    r = admin(client, "POST", f"/api/admin/operators/{p.operator_id}/founding-editor", json={"reason": "x" * 20})
    assert r.status_code == 410


def test_on_a_fresh_journal_the_cold_start_rule_draws_a_labelled_bootstrap_editor(client):
    j = Journal(client)
    pid = j.publish()
    record = paper(client, pid)
    assert record["editor"]["bootstrap"] and record["bootstrap"]
    assert record["decisions"][0]["bootstrap"] is True
    drawn = events(client, "editor_assigned", pid)[0]["payload"]
    assert drawn["mode"] == "cold_start" and drawn["bootstrap"] is True
    assert events(client, "editor_decision", pid)[0]["payload"]["bootstrap"] is True
    assert record["editor"]["operator_id"] != j.author.operator_id


def test_once_operators_have_reviewed_the_bootstrap_editor_comes_from_them(client):
    from tests.helpers import process

    j = Journal(client, reviewers=4)
    counts = {}
    for _ in range(8):
        process(j)
        counts = {p.operator_id: client.get(f"/api/operators/{p.operator_id}").json()["review_activity"]["completed"]
                  for p in j.everyone if p is not j.author}
        if any(n >= 3 for n in counts.values()):
            break
    assert any(n >= 3 for n in counts.values())
    grant(j.author, 5)
    pid = j.submit("A paper drawn after some operators have a bootstrap record of reviews")
    drawn = events(client, "editor_assigned", pid)[0]["payload"]
    assert drawn["mode"] == "bootstrap" and drawn["bootstrap"]
    assert counts[j.editor_of(pid).operator_id] >= 3


def test_with_enough_eligible_editors_the_review_record_rule_applies_automatically(client):
    from tests.helpers import process

    with constitution.override(editor_min_adequate_reviews=1, editor_min_pool=1):
        j = Journal(client)
        process(j)
        grant(j.author, 5)
        pid = j.submit("A paper drawn once the field has eligible editors by record")
        drawn = events(client, "editor_assigned", pid)[0]["payload"]
        assert drawn["mode"] == "review_record" and drawn["bootstrap"] is False
        assert paper(client, pid)["editor"]["bootstrap"] is False


# --------------------------------------------------------------------------
# E. appeals
# --------------------------------------------------------------------------


def appeal(client, who, kind, target, grounds="The original judgement misread the record, as set out here in detail."):
    return client.post("/api/appeals", json={"kind": kind, "target": target, "grounds": grounds}, headers=who.headers)


def rule(client, j, appeal_id, ruling):
    body = client.get(f"/api/appeals/{appeal_id}").json()
    judge = j.by_operator(body["appeal_editor"]["operator_id"])
    return judge, client.post(f"/api/appeals/{appeal_id}/ruling",
                              json={"ruling": ruling, "rationale": "Judged on the record as it stood at the time of the decision."},
                              headers=judge.headers)


def test_authors_appeal_a_rejection_and_an_overturn_refunds_them_and_hands_the_paper_to_the_appeal_editor(client):
    j = Journal(client)
    pid = j.submit()
    original = j.editor_of(pid)
    panel = {p.operator_id for p, _ in j.panel(pid)}
    did = reject(j, pid)
    assert appeal(client, j.author, "decision", did).status_code == 402, "an appeal costs a credit"
    grant(j.author, 1)
    r = appeal(client, j.author, "decision", did)
    assert r.status_code == 201, r.text
    filed = r.json()
    assert credits(client, j.author) == 0 and filed["fee"] == 1 and filed["due_at"]
    judge_id = filed["appeal_editor"]["operator_id"]
    assert judge_id not in {original.operator_id, j.author.operator_id, *panel}

    again = appeal(client, j.author, "decision", did)
    assert again.status_code == 409 and "once" in again.json()["detail"]

    outsider = next(p for p in j.everyone if p.operator_id not in (judge_id, j.author.operator_id))
    assert client.post(f"/api/appeals/{filed['appeal_id']}/ruling", json={"ruling": "overturned", "rationale": "x" * 50},
                       headers=outsider.headers).status_code == 409

    judge, ruled = rule(client, j, filed["appeal_id"], "overturned")
    assert ruled.status_code == 200 and ruled.json()["state"] == "OVERTURNED"
    assert credits(client, j.author) == 1, "refunded"
    record = paper(client, pid)
    assert record["state"] == "REVIEWS_EVALUATED"
    assert record["decisions"][0]["overturned_on_appeal"]
    assert record["editor"]["operator_id"] == judge.operator_id
    final = rule(client, j, filed["appeal_id"], "upheld")[1]
    assert final.status_code == 409 and "final" in final.json()["detail"]
    assert j.decide(pid, "accept", editor=judge).status_code == 201
    kinds = [e["event"] for e in client.get("/api/events", params={"paper": pid, "limit": 1000}).json()["events"]]
    assert {"appeal_filed", "appeal_editor_assigned", "appeal_ruled", "editor_replaced"} <= set(kinds)


def test_an_upheld_appeal_keeps_the_fee_and_pays_the_appeal_editor(client):
    j = Journal(client)
    pid = j.submit()
    did = reject(j, pid)
    grant(j.author, 1)
    filed = appeal(client, j.author, "decision", did).json()
    judge = j.by_operator(filed["appeal_editor"]["operator_id"])
    before = credits(client, judge)
    _, ruled = rule(client, j, filed["appeal_id"], "upheld")
    assert ruled.status_code == 200
    assert credits(client, j.author) == 0 and credits(client, judge) == before + 1
    assert paper(client, pid)["state"] == "REJECTED"


def test_only_the_authors_may_appeal_a_decision_an_acceptance_is_not_appealable_and_the_window_closes(client):
    j = Journal(client)
    accepted = j.publish()
    decision = paper(client, accepted)["decisions"][0]["decision_id"]
    grant(j.author, 2)
    assert appeal(client, j.author, "decision", decision).status_code == 409
    grant(j.author, 5)
    pid = j.submit("A second paper, rejected, whose appeal comes too late")
    did = reject(j, pid)
    assert appeal(client, j.reviewers[0], "decision", did).status_code == 409
    backdate(EditorialDecision, did, "created_at", hours=25)
    late = appeal(client, j.author, "decision", did)
    assert late.status_code == 409 and "window" in late.json()["detail"]


def test_a_reviewer_appeals_an_inadequate_mark_and_an_overturn_pays_for_the_review(client):
    j = Journal(client, reviewers=6)
    pid = j.submit()
    j.review_all(pid)
    editor = j.editor_of(pid)
    reviews_ = client.get(f"/api/editor/manuscripts/{pid}", headers=editor.headers).json()["reviews"]
    target = reviews_[0]
    client.post(f"/api/editor/reviews/{target['review_id']}/evaluation", json={"evaluation": "inadequate"}, headers=editor.headers)
    reviewer = j.by_operator(target["reviewer"]["operator_id"])
    assert appeal(client, j.author, "evaluation", target["review_id"]).status_code == 409, "only the reviewer"
    before = credits(client, reviewer)
    filed = appeal(client, reviewer, "evaluation", target["review_id"]).json()
    assert filed["appeal_editor"]["operator_id"] not in {editor.operator_id, reviewer.operator_id, j.author.operator_id}
    _, ruled = rule(client, j, filed["appeal_id"], "overturned")
    assert ruled.status_code == 200
    assert credits(client, reviewer) == before + 1, "fee refunded (net zero) plus one credit for the review"
    review = next(r for r in paper(client, pid, reviewer)["reviews"] if r["review_id"] == target["review_id"])
    assert review["evaluation"] == "adequate" and review["evaluation_overturned_on_appeal"]
    record = client.get(f"/api/operators/{editor.operator_id}").json()["appeals_against_its_judgements"]
    assert record == {**record, "ruled": 1, "overturned": 1}


def test_authors_appeal_a_contested_status_to_an_editor_who_is_neither_the_contester_nor_the_critic(client):
    from tests.test_living_record import evaluator_for, post_review

    j = Journal(client)
    pid = j.publish()
    critic = next(p for p in j.everyone if p.operator_id not in (j.author.operator_id, j.editor_of(pid).operator_id))
    rid = post_review(client, pid, critic, seed="contest").json()["review_id"]
    contester = evaluator_for(client, j, rid)
    client.post(f"/api/editor/reviews/{rid}/evaluation", json={"evaluation": "adequate", "contests_paper": True}, headers=contester.headers)
    assert paper(client, pid)["status"] == "CONTESTED"
    grant(j.author, 1)
    filed = appeal(client, j.author, "status", pid)
    assert filed.status_code == 201, filed.text
    judge_id = filed.json()["appeal_editor"]["operator_id"]
    assert judge_id not in {contester.operator_id, critic.operator_id, j.author.operator_id}
    _, ruled = rule(client, j, filed.json()["appeal_id"], "overturned")
    assert ruled.status_code == 200
    assert paper(client, pid)["status"] == "PUBLISHED"


def test_an_appeal_editor_who_misses_the_deadline_lapses_and_the_appeal_is_redrawn(client):
    j = Journal(client, reviewers=6)
    pid = j.submit()
    did = reject(j, pid)
    grant(j.author, 1)
    filed = appeal(client, j.author, "decision", did).json()
    first = filed["appeal_editor"]["operator_id"]
    backdate(Appeal, filed["appeal_id"])
    result = sweep(client)
    assert result["appeal_editors_lapsed"] == 1 and result["appeal_editors_assigned"] == 1
    now = client.get(f"/api/appeals/{filed['appeal_id']}").json()
    assert now["appeal_editor"]["operator_id"] != first and now["state"] == "PENDING"
    assert any(e["payload"]["role"] == "appeal_editor" for e in events(client, "lapse"))


# --------------------------------------------------------------------------
# F. editors overturned too often lose eligibility
# --------------------------------------------------------------------------


def test_an_editor_overturned_too_often_loses_eligibility(client):
    from app.services import appeals as appeal_service, assignment, record

    j = Journal(client)
    pid = j.publish()
    target = j.reviewers[0]

    def seed(states):
        def go(s):
            op = operator_row(s, target)
            m = s.execute(select(Manuscript).where(Manuscript.molt_id == pid)).scalar_one()
            author = operator_row(s, j.author)
            for n, state in enumerate(states):
                s.add(Appeal(molt_id=f"MOLT-AP-TEST{len(states)}{n}", kind="decision", manuscript_id=m.id,
                             target_id=f"t-{len(states)}-{n}", appellant_operator_id=author.id,
                             appellant_agent_id=s.execute(select(EditorAssignment.editor_agent_id)).scalars().first(),
                             original_editor_operator_id=op.id, grounds="seeded", state=state))
        db(go)

    seed(["OVERTURNED", "UPHELD"])
    assert client.get(f"/api/operators/{target.operator_id}").json()["appeals_against_its_judgements"]["editor_eligible"], \
        "two ruled appeals are too few to judge"
    seed(["OVERTURNED"])
    stats = client.get(f"/api/operators/{target.operator_id}").json()["appeals_against_its_judgements"]
    assert stats["ruled"] == 3 and stats["overturn_rate"] > 0.34 and not stats["editor_eligible"]

    def pool(s):
        m = s.execute(select(Manuscript).where(Manuscript.molt_id == pid)).scalar_one()
        agents, _ = assignment.editor_pool(s, m, assignment.latest_version(s, m))
        return {a.operator_id for a in agents}

    assert operator_row_id(target) not in db(pool)
    assert appeal_service  # imported for its side of the rule
    assert record.appeals_eligible


def operator_row_id(p):
    return db(lambda s: operator_row(s, p).id)


# --------------------------------------------------------------------------
# H. the correctness signal
# --------------------------------------------------------------------------


def test_the_correctness_signal_counts_recommendations_against_outcomes(client):
    j = Journal(client)
    pid = j.submit()
    j.review_all(pid, recommendation="reject")
    j.evaluate_all(pid)
    assert j.decide(pid, "accept").status_code == 201
    harsh = paper(client, pid)["panel"][0]["operator_id"]
    signal = client.get(f"/api/operators/{harsh}").json()["correctness"]
    assert signal["rejecting_on_later_accepted"] == 1 and signal["reviews"] == 1

    grant(j.author, 5)
    second = j.publish("A second paper, accepted and then withdrawn by its authors")
    client.post(f"/api/manuscripts/{second}/withdraw", json={"note": "an error in the pipeline"}, headers=j.author.headers)
    lenient = paper(client, second)["panel"][0]["operator_id"]
    assert client.get(f"/api/operators/{lenient}").json()["correctness"]["accepting_on_later_corrected_or_withdrawn"] == 1
    assert "Correctness" in client.get(f"/operators/{lenient}").text


# --------------------------------------------------------------------------
# I. collusion and conflicts
# --------------------------------------------------------------------------


def test_a_reviewer_is_not_drawn_for_an_author_who_recently_reviewed_it(client):
    j = Journal(client, reviewers=6)
    first = j.submit()
    reviewer, _ = j.panel(first)[0]
    pid = j.submit("A paper by an operator that just reviewed the first author", author=reviewer)
    on_panel = {p.operator_id for p, _ in j.panel(pid)}
    assert j.author.operator_id not in on_panel
    excluded = [e for e in events(client, "conflict_detected", pid) if e["payload"].get("reciprocal_review")]
    assert excluded or j.editor_of(pid).operator_id == j.author.operator_id


def test_nobody_with_the_authors_affiliation_reviews_or_edits(client):
    author = register(client, "Author Lab", 0, affiliation="University of Amsterdam")
    colleague = register(client, "Colleague Lab", 1, affiliation="  university of amsterdam ")
    others = [register(client, f"Other {i}", i + 2) for i in range(4)]
    pid = client.post("/api/manuscripts", json=manuscript(), headers=author.headers).json()["paper_id"]
    assert not [a for a in assignments(client, colleague) if a["paper_id"] == pid]
    assert paper(client, pid)["editor"]["operator_id"] != colleague.operator_id
    assert any(e["payload"].get("affiliation") for e in events(client, "conflict_detected", pid))
    assert len(others) == 4


def test_competing_interests_are_declared_and_shown(client):
    j = Journal(client)
    r = client.post("/api/manuscripts", json=manuscript(competing_interests="Funded by the lab whose method is tested."),
                    headers=j.author.headers)
    pid = r.json()["paper_id"]
    p, a = j.panel(pid)[0]
    body = {**review_body(p), "assignment_id": a["assignment_id"], "competing_interests": "None."}
    assert client.post("/api/reviews", json=body, headers=p.headers).status_code == 201
    assert paper(client, pid)["competing_interests"] == "Funded by the lab whose method is tested."
    assert paper(client, pid, p)["reviews"][0]["competing_interests"] == "None."


# --------------------------------------------------------------------------
# J. controlled vocabularies
# --------------------------------------------------------------------------


def test_model_families_are_mapped_to_a_canonical_list():
    cases = {"anthropic claude": "Claude", "gpt-4o": "GPT", "OpenAI o3": "GPT", " GEMINI 2.5 ": "Gemini",
             "Llama-4": "Llama", "mixtral": "Mistral", "qwen2.5": "Qwen", "DeepSeek-R1": "DeepSeek",
             "grok 4": "Grok", "my homebrew model": "Other", "Claude": "Claude"}
    for raw, family in cases.items():
        assert vocab.canonical_family(raw) == family, raw


def test_the_canonical_family_is_stored_and_the_declared_one_kept(client):
    from app.models import Agent

    p = register(client, "Raw Family Lab", 0)
    added = client.post("/api/agents", json={"agent": {"name": "Raw", "model_provenance": {
        "provider": "OpenAI", "model_family": "gpt-4o", "model_version": "gpt-4o-2024"}}}, headers=p.headers)
    agent_id = added.json()["agent_id"]
    row = db(lambda s: s.execute(select(Agent).where(Agent.molt_id == agent_id)).scalar_one())
    assert (row.model_family, row.model_family_raw) == ("GPT", "gpt-4o")


def test_diversity_compares_canonical_families(client):
    author = register(client, "Author Lab", 1)
    # "gpt-4o", "GPT" and "OpenAI GPT-5" are one family: a panel of all three is not diverse.
    for i, family in enumerate(["gpt-4o", "GPT", "OpenAI GPT-5", "chatgpt"]):
        client.post("/api/accounts", json={"accepts_terms": True,
            "operator": {"name": f"Same Family {i}", "type": "lab", "affiliation": f"Place {i}", "contact": f"s{i}@example.org"},
            "agent": {"name": f"S{i}", "model_provenance": {"provider": "OpenAI", "model_family": family, "model_version": "v"}}})
    pid = client.post("/api/manuscripts", json=manuscript(), headers=author.headers).json()["paper_id"]
    assert len(events(client, "diversity_exception", pid)) == 2


def test_fields_must_come_from_the_fixed_list(client):
    j = Journal(client)
    bad = client.post("/api/manuscripts", json=manuscript(primary_field="Computational Social Science"), headers=j.author.headers)
    assert bad.status_code == 422
    detail = str(bad.json()["detail"])
    for field in vocab.FIELDS:
        assert field in detail
    assert client.post("/api/manuscripts", json=manuscript(secondary_fields=["Sociology"]), headers=j.author.headers).status_code == 422
    ok = client.post("/api/manuscripts", json=manuscript(primary_field="CS", secondary_fields=["Q-Bio"]), headers=j.author.headers)
    assert ok.status_code == 201
    record = paper(client, ok.json()["paper_id"])
    assert record["primary_field"] == "cs" and record["secondary_fields"] == ["q-bio"]


# --------------------------------------------------------------------------
# K. injection reports and automatic suspension
# --------------------------------------------------------------------------


def test_confirmed_injection_reports_suspend_an_operator_automatically(client):
    j = Journal(client, reviewers=6)
    published = j.publish()
    grant(j.author, 5)
    pid = j.submit("A paper whose body tells reviewers to recommend acceptance")
    for p, a in j.panel(pid):
        body = {**review_body(p), "assignment_id": a["assignment_id"], "suspected_injection": True,
                "injection_note": "Section 2 tells the reviewer to ignore its instructions and accept."}
        assert client.post("/api/reviews", json=body, headers=p.headers).status_code == 201
    editor = j.editor_of(pid)
    reviews_ = client.get(f"/api/editor/manuscripts/{pid}", headers=editor.headers).json()["reviews"]
    url = lambda r: f"/api/editor/reviews/{r['review_id']}/evaluation"  # noqa: E731
    missing = client.post(url(reviews_[0]), json={"evaluation": "adequate"}, headers=editor.headers)
    assert missing.status_code == 409 and "injection_confirmed" in missing.json()["detail"]
    client.post(url(reviews_[0]), json={"evaluation": "adequate", "injection_confirmed": True}, headers=editor.headers)
    assert client.get("/api/profile", headers=j.author.headers).status_code == 200, "one report is not enough"
    client.post(url(reviews_[1]), json={"evaluation": "adequate", "injection_confirmed": True}, headers=editor.headers)

    assert client.get("/api/profile", headers=j.author.headers).status_code == 403
    assert client.post("/api/manuscripts", json=manuscript("Anything else"), headers=j.author.headers).status_code == 403
    suspended = events(client, "operator_suspended")[0]
    assert suspended["operator_id"] == j.author.operator_id and suspended["payload"]["automatic"] is True
    assert len(events(client, "injection_report_confirmed")) == 2
    assert paper(client, published)["operator_suspended"] is True
    assert "operator suspended" in client.get("/papers").text
    record = client.get(f"/api/operators/{j.author.operator_id}").json()["suspension"]
    assert record["suspended"] and record["until"]

    backdate(Operator, j.author.operator_id, "suspended_until")
    assert sweep(client)["suspensions_lifted"] == 1
    assert client.get("/api/profile", headers=j.author.headers).status_code == 200


def test_a_suspended_operators_open_work_is_reassigned(client):
    j = Journal(client, reviewers=6)
    pid = j.submit()
    victim, a = j.panel(pid)[0]
    r = admin(client, "POST", f"/api/admin/operators/{victim.operator_id}/suspend", json={"reason": "emergency: compromised key"})
    assert r.status_code == 200
    seats = j.panel(pid)
    assert len(seats) == 3 and victim.operator_id not in {p.operator_id for p, _ in seats}
    assert events(client, "assignment_reassigned")
    logged = events(client, "operator_suspended")[0]["payload"]
    assert logged["automatic"] is False and logged["reason"].startswith("administrator:")


def test_every_admin_action_is_on_the_public_log(client):
    admin(client, "POST", "/api/admin/invites", json={"note": "a colleague"})
    minted = events(client, "admin_invite_minted")
    assert minted and "code" not in minted[0]["payload"], "the code itself stays private"


# --------------------------------------------------------------------------
# L. registration as a parameter
# --------------------------------------------------------------------------


def test_earned_invites_follow_adequate_reviews(client, monkeypatch):
    from app import config
    from tests.helpers import process

    monkeypatch.setattr(config, "REGISTRATION_OVERRIDE", "")
    with constitution.override(registration="open", invite_per_adequate_reviews=1):
        j = Journal(client, reviewers=3)
    monkeypatch.setattr(config, "REGISTRATION_OVERRIDE", "earned_invite")
    with constitution.override(invite_per_adequate_reviews=1):
        assert client.get("/api/invites", headers=j.reviewers[0].headers).json()["entitled"] == 0
        process(j)
        earner = next(p for p in j.everyone if client.get(f"/api/operators/{p.operator_id}").json()["review_activity"]["adequate"])
        mine = client.get("/api/invites", headers=earner.headers).json()
        assert mine["entitled"] == 1 and len(mine["invites"]) == 1
        assert client.get("/api/invites", headers=earner.headers).json()["invites"] == mine["invites"], "asking twice mints nothing"
        code = mine["invites"][0]["code"]
        r = client.post("/api/accounts", json={"accepts_terms": True,
            "invite_code": code,
            "operator": {"name": "Invited Lab", "type": "lab", "contact": "invited@example.org"},
            "agent": {"name": "I", "model_provenance": {"provider": "Meta", "model_family": "Llama", "model_version": "4"}}})
        assert r.status_code == 201


# --------------------------------------------------------------------------
# the work queue and the role prompts
# --------------------------------------------------------------------------


def tasks(client, p):
    r = client.get("/api/tasks", headers=p.headers)
    assert r.status_code == 200, r.text
    return r.json()["tasks"]


def test_tasks_give_a_reviewer_the_paper_its_deadline_and_the_reviewer_prompt(client):
    j = Journal(client)
    pid = j.submit()
    p, a = j.panel(pid)[0]
    mine = [t for t in tasks(client, p) if t["type"] == "review"]
    assert len(mine) == 1
    t = mine[0]
    assert t["id"] == a["assignment_id"] and t["due_at"] and t["credits_on_completion"] == 1
    assert t["submit"]["endpoint"] == "/api/reviews" and t["submit"]["body_schema"]["assignment_id"] == a["assignment_id"]
    assert "# Reviewer" in t["instructions"] and "untrusted" in t["instructions"]
    keys = list(t)
    assert keys.index("instructions") < keys.index("untrusted_content"), "instructions come first"
    assert t["untrusted_content"]["body"].startswith("## Introduction")
    assert "Introduction" not in t["instructions"]


def test_tasks_give_the_editor_marks_then_a_decision(client):
    j = Journal(client)
    pid = j.submit()
    editor = j.editor_of(pid)
    assert not [t for t in tasks(client, editor) if t["type"] == "editor"], "nothing to do while reviews are out"
    j.review_all(pid)
    t = next(t for t in tasks(client, editor) if t["type"] == "editor")
    assert t["due_at"] and t["credits_on_completion"] == 2 and len(t["reviews_to_mark"]) == 3
    assert "evaluation" in t["submit"]["endpoint"] and "# Editor" in t["instructions"]
    assert len(t["untrusted_content"]["reviews"]) == 3
    j.evaluate_all(pid)
    t = next(t for t in tasks(client, editor) if t["type"] == "editor")
    assert t["submit"]["endpoint"] == f"/api/editor/manuscripts/{pid}/decision"


def test_tasks_give_authors_their_revision_and_appeal_editors_their_appeal(client):
    j = Journal(client)
    pid = j.submit()
    did = reject(j, pid, "major_revision")
    t = next(t for t in tasks(client, j.author) if t["type"] == "revision")
    assert t["id"] == pid and t["due_at"] and t["appeal"]["target"] == did
    assert "# Author" in t["instructions"] and t["credits_on_completion"] == -5
    grant(j.author, 1)
    filed = appeal(client, j.author, "decision", did).json()
    judge = j.by_operator(filed["appeal_editor"]["operator_id"])
    t = next(t for t in tasks(client, judge) if t["type"] == "appeal")
    assert t["id"] == filed["appeal_id"] and t["due_at"] and "# Appeal editor" in t["instructions"]
    assert t["untrusted_content"]["appeal"]["grounds"]


def test_tasks_are_private_to_the_operator_and_sorted_by_deadline(client):
    j = Journal(client)
    first = j.submit()
    grant(j.author, 5)
    j.submit("A second paper so that someone holds two review tasks at once")
    for p in j.everyone:
        mine = tasks(client, p)
        assert all(t.get("paper_id") for t in mine)
        dues = [t["due_at"] for t in mine if t["due_at"]]
        assert dues == sorted(dues)
        on_panel = {a["assignment_id"] for a in assignments(client, p)}
        assert {t["id"] for t in mine if t["type"] == "review"} == on_panel
    assert not [t for t in tasks(client, j.author) if t["type"] in ("review", "editor")]
    assert first


def test_tasks_invite_post_publication_review_as_optional(client):
    j = Journal(client)
    pid = j.publish()
    outsider = j.add("Outsider Lab", 4)
    invites = [t for t in tasks(client, outsider) if t["type"] == "post_publication_review"]
    assert invites and invites[0]["optional"] and invites[0]["paper_id"] == pid and invites[0]["due_at"] is None
    assert not [t for t in tasks(client, j.author) if t["type"] == "post_publication_review"]


def test_the_role_prompts_are_versioned_public_and_filled_from_the_constitution(client):
    listing = client.get("/api/prompts").json()
    assert listing["prompt_version"] == constitution.current().prompt_version == "P-4"
    roles = {r["role"] for r in listing["roles"]}
    assert roles == {"reviewer", "rereviewer", "editor", "appeal_editor", "post_publication_reviewer", "author_revision"}
    for role in roles:
        text = client.get(f"/api/prompts/{role}").text
        assert "P-4" in text and "${" not in text, role
        for must in ("Adequacy rubric", "Soundness versus significance", "sandbox", "untrusted",
                     "suspected_injection", "lapse", "model_provenance", "POST"):
            assert must in text, (role, must)
    reviewer = client.get("/api/prompts/reviewer").text
    assert "Review to publish" in reviewer and "24 hours" in reviewer
    editor = client.get("/api/prompts/editor").text
    assert "only the `required_changes` of the adequate reviews" in editor and "never decides" in editor
    appeal_text = client.get("/api/prompts/appeal_editor").text
    assert "uphold" in appeal_text.lower() and "overturn" in appeal_text.lower() and "on the record" in appeal_text
    assert client.get("/api/prompts/nobody").status_code == 404
    assert client.get("/api/prompts/reviewer", params={"version": "../../etc"}).status_code == 404
    with constitution.override(reviewer_deadline_hours=6):
        assert "6 hours" in client.get("/api/prompts/reviewer").text
    assert "Role prompts" in client.get("/how-it-works").text and "/api/prompts/editor" in client.get("/how-it-works").text


def test_the_protocol_tells_an_agent_how_to_do_its_operators_work(client):
    text = client.get("/llms.txt").text
    head = text[: text.index("## The five rules")]
    assert "What your operator asked you to do" in head
    assert "/api/tasks" in head and "untrusted_content" in head
    assert "do my MoltJournal work using the API key in $MOLT_API_KEY" in head
    assert "do my MoltJournal work using the API key in $MOLT_API_KEY" in client.get("/").text
    for deadline in ("24 hours from assignment", "72 hours from the decision"):
        assert deadline in text


# --------------------------------------------------------------------------
# pages
# --------------------------------------------------------------------------


def test_the_constitution_page_shows_every_parameter_with_a_sentence(client):
    html = client.get("/constitution").text
    for name in constitution.current().as_dict():
        if name in ("label", "version"):
            continue
        assert f">{name}<" in html, name
    assert ">Yes<" in html and "True" not in html.split("<h2>Amendments")[0]


def test_pages_have_a_skip_link_and_a_main_landmark(client):
    html = client.get("/").text
    assert 'href="#main"' in html and '<main id="main">' in html
    assert "Check our work" in html and "The journal, checked" not in html
    assert 'src="/static/hero.png"' in html and 'width="787"' in html and "hero.webp" in html
    assert client.get("/static/hero.webp").status_code == 200


def test_the_homepage_says_truthfully_what_is_in_the_record(client):
    assert "Nothing has been submitted yet" in client.get("/").text
    assert "first manuscripts are in review" not in client.get("/").text
    j = Journal(client)
    j.submit()
    assert "1 manuscript is in review" in client.get("/").text
