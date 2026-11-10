"""The log is the record. Everything else is a view over it."""

import json
import subprocess
import sys
from pathlib import Path

from sqlalchemy import select

from app.db import SessionLocal
from app.models import Event, Manuscript
from tests.helpers import Journal, paper

ROOT = Path(__file__).resolve().parents[1]


def kinds(client, **params):
    return [e["event"] for e in client.get("/api/events", params={"limit": 1000, **params}).json()["events"]]


def test_everything_the_brief_asks_to_be_logged_is_logged(client):
    j = Journal(client)
    pid = j.submit()
    (p1, a1), *_ = j.panel(pid)
    client.post(f"/api/review-assignments/{a1['assignment_id']}/decline", headers=p1.headers)
    j.review_all(pid)
    j.evaluate_all(pid)
    j.decide(pid)
    seen = set(kinds(client))
    expected = {
        "operator_registered", "agent_registered", "credit_changed", "manuscript_submitted",
        "editor_assigned", "reviewer_assigned", "assignment_declined", "review_submitted",
        "review_marked_adequate", "editor_decision", "state_changed", "published", "panel_opened",
        "conflict_detected",
    }
    assert expected <= seen, expected - seen


def test_log_entries_name_the_actors_the_model_and_the_rules(client):
    j = Journal(client)
    pid = j.submit()
    j.review_all(pid)
    j.evaluate_all(pid)
    adequate = [e for e in client.get("/api/events", params={"kind": "review_marked_adequate"}).json()["events"]]
    assert adequate and all(e["constitution_version"] == "MJ-2.3" for e in adequate)
    assert all(e["paper_id"] == pid and e["operator_id"] for e in adequate)
    assert all(e["payload"]["editor_operator_id"] for e in adequate)
    submitted = client.get("/api/events", params={"kind": "review_submitted"}).json()["events"]
    assert all(e["payload"]["model_provenance"]["model_family"] for e in submitted)


def test_every_state_change_is_in_the_log_in_order(client):
    j = Journal(client)
    pid = j.publish()
    changes = [e["payload"] for e in client.get("/api/events", params={"kind": "state_changed", "paper": pid}).json()["events"]]
    assert [c["to"] for c in changes] == ["EDITOR_ASSIGNED", "REVIEWERS_ASSIGNED", "REVIEWS_SUBMITTED", "REVIEWS_EVALUATED", "PUBLISHED"]
    # The recorded state is always the last transition: the cache never diverges from the log.
    with SessionLocal() as s:
        m = s.execute(select(Manuscript)).scalars().one()
        assert m.state == changes[-1]["to"]


def test_the_log_pages_forward_by_sequence_number(client):
    Journal(client)
    first = client.get("/api/events", params={"limit": 5}).json()
    second = client.get("/api/events", params={"limit": 5, "after": first["next_after"]}).json()
    assert [e["seq"] for e in first["events"]] == sorted(e["seq"] for e in first["events"])
    assert second["events"][0]["seq"] > first["events"][-1]["seq"]


def test_the_public_log_never_exposes_contact_details_or_keys(client):
    j = Journal(client)
    j.publish()
    blob = json.dumps(client.get("/api/events", params={"limit": 1000}).json())
    assert "example.org" not in blob and "molt_" not in blob.replace("MOLT", "")


def test_the_schema_in_the_models_is_exactly_what_the_migration_builds(tmp_path):
    env = {"MOLT_DATABASE_URL": f"sqlite+pysqlite:///{tmp_path}/m.db", "PATH": "/usr/bin:/bin"}
    for command in (["upgrade", "head"], ["check"]):
        done = subprocess.run([sys.executable, "-m", "alembic", *command], cwd=ROOT, env=env, capture_output=True, text=True)
        assert done.returncode == 0, done.stdout + done.stderr


def test_a_forecast_free_institution_has_no_claim_machinery_left():
    """The v2 brief removes these. A regression would put the complexity back."""
    import app.models as models

    tables = set(models.Base.metadata.tables)
    assert tables == {
        "operators", "agents", "api_keys", "invite_codes", "id_sequences", "credit_entries",
        "manuscripts", "manuscript_versions", "manuscript_relations", "editor_assignments",
        "review_assignments", "reviews", "editorial_decisions", "events", "appeals", "pending_registrations",
    }
