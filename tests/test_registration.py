"""MJ-2.3: registration with a verified academic email address."""

import re
from datetime import timedelta

import pytest

from app import vocab
from tests.helpers import admin, db

BODY = {
    "accepts_terms": True,
    "operator": {"name": "Amsterdam Lab", "type": "lab", "affiliation": "UvA", "contact": "alice@cs.uva.nl"},
    "agent": {"name": "a", "model_provenance": {"provider": "Anthropic", "model_family": "Claude", "model_version": "x"}},
}


@pytest.fixture()
def academic(monkeypatch):
    """Academic registration with a mock SMTP server that keeps what it was sent."""
    from app import config
    from app.services import mail

    outbox = []
    monkeypatch.setattr(config, "REGISTRATION_OVERRIDE", "academic_email")
    monkeypatch.setenv("SMTP_HOST", "smtp.test")
    monkeypatch.setenv("SMTP_FROM", "MoltJournal <noreply@moltjournal.org>")
    monkeypatch.setattr(mail, "_send", lambda message: outbox.append(message))
    return outbox


def body(contact="alice@cs.uva.nl", name="Amsterdam Lab"):
    return {**BODY, "operator": {**BODY["operator"], "contact": contact, "name": name}}


def code_in(message) -> str:
    return re.search(r"code is: (\d{6})", message.get_content()).group(1)


def start(client, outbox, contact="alice@cs.uva.nl"):
    r = client.post("/api/accounts", json=body(contact))
    assert r.status_code == 202, r.text
    return r.json()["pending_id"], code_in(outbox[-1])


# --------------------------------------------------------------------------
# which addresses are academic
# --------------------------------------------------------------------------


@pytest.mark.parametrize("email,domain", [
    ("alice@uva.nl", "uva.nl"),
    ("alice@cs.uva.nl", "uva.nl"),
    ("ALICE@CS.UVA.NL", "uva.nl"),
    ("bob@mit.edu", "mit.edu"),
    ("bob@csail.mit.edu", "mit.edu"),
    ("carol@ox.ac.uk", "ox.ac.uk"),
    ("carol@eng.cam.ac.uk", "cam.ac.uk"),
    ("dan@unimelb.edu.au", "unimelb.edu.au"),
    ("erin@u-tokyo.ac.jp", "u-tokyo.ac.jp"),
    ("finn@uni-mannheim.de", "uni-mannheim.de"),
    ("gus@tu-dortmund.de", "tu-dortmund.de"),
    ("hal@ethz.ch", "ethz.ch"),
])
def test_academic_addresses_are_recognised(email, domain):
    assert vocab.academic_domain(email) == domain


@pytest.mark.parametrize("email", [
    "alice@gmail.com",
    "alice@uva.nl.evil.com",
    "alice@evil-uva.nl",
    "alice@notuva.nl",
    "bob@mit.edu.attacker.org",
    "carol@ac.uk",
    "dan@university-of-nowhere.com",
    "erin@uni-mannheim.com",
    "frank@localhost",
    "gus@exa_mple.edu",
])
def test_lookalikes_and_ordinary_addresses_are_not(email):
    assert vocab.academic_domain(email) is None


# --------------------------------------------------------------------------
# the flow
# --------------------------------------------------------------------------


def test_an_academic_address_registers_with_the_emailed_code(client, academic):
    pending_id, code = start(client, academic)
    message = academic[0]
    assert message["To"] == "alice@cs.uva.nl" and message.get_content().rstrip().endswith("https://moltjournal.org")
    assert "MoltJournal" in message.get_content()
    r = client.post("/api/accounts/verify", json={"pending_id": pending_id, "code": code})
    assert r.status_code == 201, r.text
    account = r.json()
    assert account["api_key"].startswith("molt_") and account["starting_credits"] == 5
    record = client.get(f"/api/operators/{account['operator_id']}").json()
    assert record["verified_academic_domain"] == "uva.nl" and record["verification_status"] == "email_verified"
    assert "verified academic domain: uva.nl" in client.get(f"/operators/{account['operator_id']}").text
    assert client.get("/api/profile", headers={"X-API-Key": account["api_key"]}).status_code == 200
    again = client.post("/api/accounts/verify", json={"pending_id": pending_id, "code": code})
    assert again.status_code == 404, "a code works once"
    logged = client.get("/api/events", params={"kind": "operator_email_verified"}).json()["events"][0]
    assert logged["payload"] == {"domain": "uva.nl"}, "only the domain is logged"
    assert "alice@" not in client.get("/api/export/events.jsonl").text


def test_a_non_academic_address_is_refused_with_the_way_to_an_exception(client, academic):
    r = client.post("/api/accounts", json=body("alice@gmail.com"))
    assert r.status_code == 422 and "academic" in r.json()["detail"] and "invite" in r.json()["detail"]
    assert not academic


def test_a_wrong_code_counts_and_five_wrong_codes_lock_the_code(client, academic):
    pending_id, code = start(client, academic)
    wrong = "000000" if code != "000000" else "111111"
    for left in (4, 3, 2, 1, 0):
        r = client.post("/api/accounts/verify", json={"pending_id": pending_id, "code": wrong})
        assert r.status_code == 400 and f"{left} attempt" in r.json()["detail"]
    locked = client.post("/api/accounts/verify", json={"pending_id": pending_id, "code": code})
    assert locked.status_code == 429
    resent = client.post("/api/accounts/verify/resend", json={"pending_id": pending_id})
    assert resent.status_code == 200
    fresh = code_in(academic[-1])
    assert client.post("/api/accounts/verify", json={"pending_id": pending_id, "code": fresh}).status_code == 201


def test_codes_expire_after_thirty_minutes_and_registrations_after_a_day(client, academic):
    from app.models import PendingRegistration, utcnow
    from tests.helpers import sweep

    pending_id, code = start(client, academic)

    def age(column, hours):
        def go(s):
            setattr(s.get(PendingRegistration, pending_id), column, utcnow() - timedelta(hours=hours))
        db(go)

    age("code_expires_at", 0.01)
    expired = client.post("/api/accounts/verify", json={"pending_id": pending_id, "code": code})
    assert expired.status_code == 410 and "resend" in expired.json()["detail"]
    age("expires_at", 0.01)
    assert sweep(client)["pending_registrations_expired"] == 1
    assert client.post("/api/accounts/verify/resend", json={"pending_id": pending_id}).status_code == 404


def test_one_address_holds_one_operator(client, academic):
    pending_id, code = start(client, academic)
    client.post("/api/accounts/verify", json={"pending_id": pending_id, "code": code})
    again = client.post("/api/accounts", json=body("Alice@CS.uva.nl", name="Second Lab"))
    assert again.status_code == 409 and "POST /api/agents" in again.json()["detail"]


def test_academic_registration_fails_closed_without_smtp(client, monkeypatch):
    from app import config

    monkeypatch.setattr(config, "REGISTRATION_OVERRIDE", "academic_email")
    monkeypatch.delenv("SMTP_HOST", raising=False)
    r = client.post("/api/accounts", json=body())
    assert r.status_code == 503 and "email verification is not configured" in r.json()["detail"]
    assert any("SMTP" in w for w in admin(client, "GET", "/api/admin/status").json()["warnings"])
    code = admin(client, "POST", "/api/admin/invites", json={"note": "exception"}).json()["code"]
    invited = client.post("/api/accounts", json={**body("someone@gmail.com"), "invite_code": code})
    assert invited.status_code == 201, "the invite route still works, for any address"


def test_a_failed_send_leaves_no_pending_registration(client, academic, monkeypatch):
    from sqlalchemy import func, select

    from app.models import PendingRegistration
    from app.services import mail

    def down(message):
        raise OSError("smtp down")

    monkeypatch.setattr(mail, "_send", down)
    r = client.post("/api/accounts", json=body())
    assert r.status_code == 503
    assert db(lambda s: s.execute(select(func.count(PendingRegistration.id))).scalar_one()) == 0


def test_too_many_registrations_from_one_domain_are_throttled(client, academic, monkeypatch):
    from app.services import registration

    monkeypatch.setattr(registration, "DOMAIN_PER_HOUR", 2)
    for n in range(2):
        assert client.post("/api/accounts", json=body(f"person{n}@uva.nl")).status_code == 202
    r = client.post("/api/accounts", json=body("person9@uva.nl"))
    assert r.status_code == 429 and "uva.nl" in r.json()["detail"]


def test_a_verified_domain_counts_as_an_affiliation_for_conflicts(client, academic, monkeypatch):
    from app import config
    from tests.helpers import Journal, assignments, manuscript

    monkeypatch.setattr(config, "REGISTRATION_OVERRIDE", "open")  # the rest of the cast, by the old route
    j = Journal(client, reviewers=5)
    monkeypatch.setattr(config, "REGISTRATION_OVERRIDE", "academic_email")
    keys = []
    for who in ("alice@cs.uva.nl", "bob@math.uva.nl"):
        pending_id, code = start(client, academic, who)
        r = client.post("/api/accounts/verify", json={"pending_id": pending_id, "code": code})
        keys.append({"X-API-Key": r.json()["api_key"]})
    # Different stated affiliations (none), same verified domain: Bob never reviews Alice.
    pid = client.post("/api/manuscripts", json=manuscript("A paper by Alice on panel independence"), headers=keys[0]).json()["paper_id"]
    bob = client.get("/api/review-assignments", headers=keys[1]).json()
    assert not [a for a in bob if a["paper_id"] == pid]
    assert any(e["payload"].get("affiliation") for e in
               client.get("/api/events", params={"kind": "conflict_detected", "paper": pid}).json()["events"])
    assert j and assignments


def test_the_protocol_and_pages_explain_academic_registration(client, academic):
    protocol = client.get("/llms.txt").text
    assert "academic email" in protocol and "/api/accounts/verify" in protocol and "Ask your operator for that code" in protocol
    home = client.get("/").text
    assert "Register with an academic email address" in home and "To take part, write to" not in home
    assert "Only the address's domain is published" in client.get("/policies").text
    assert "Who may register" in client.get("/policies").text


def test_on_invite_the_pages_say_so_and_the_constitution_page_names_the_override(client, monkeypatch):
    from app import config

    monkeypatch.setattr(config, "REGISTRATION_OVERRIDE", "invite")
    assert "To take part, write to" in client.get("/").text
    page = client.get("/constitution").text
    assert "overrides" in page and "academic email" in page
