"""The rules are data. Changing one is a one-line change, and history keeps its own rules."""

import json

import pytest

from app import constitution
from app.constitution import Constitution, PRESETS
from tests.helpers import Journal, credits, paper


def test_the_canonical_rules_carry_the_plain_version_label():
    assert Constitution().label == "MJ-2.2"
    assert constitution.current().label == "MJ-2.2"


def test_a_variant_says_exactly_what_it_changes():
    c = Constitution(review_credits=False, model_diversity=False)
    assert c.label == "MJ-2.2+review_credits=False,model_diversity=False"


def test_every_preset_differs_from_the_canonical_rules_in_exactly_one_way():
    base = Constitution().as_dict()
    for name, c in PRESETS.items():
        if name == "MJ-2.2":
            continue
        # submission_cost is derived from the others, not a rule of its own
        differing = [k for k, v in c.as_dict().items() if k not in ("label", "submission_cost") and v != base[k]]
        assert len(differing) == 1, (name, differing)


def test_a_brief_style_yaml_file_loads(tmp_path):
    path = tmp_path / "lab.yaml"
    path.write_text(
        "review_credits: true\nreview_quality_gate: false\nreviews_required: 2\n"
        "editor_selection:\n  mode: random\nmodel_diversity:\n  enabled: false\n"
        "significance:\n  separate_from_acceptance: true\npostpublication_review:\n  enabled: true\n"
    )
    c = constitution.load(str(path))
    assert (c.review_quality_gate, c.reviews_required, c.editor_selection, c.model_diversity) == (False, 2, "random", False)
    assert "review_quality_gate=False" in c.label


def test_a_json_file_and_a_preset_name_both_load(tmp_path):
    path = tmp_path / "c.json"
    path.write_text(json.dumps({"reviews_required": 5}))
    assert constitution.load(str(path)).reviews_required == 5
    assert constitution.load("no-credits").review_credits is False
    assert constitution.load("") is PRESETS["MJ-2.2"]


@pytest.mark.parametrize("bad", [{"editor_selection": "lottery"}, {"reviews_required": 0}, {"nonsense": 1}, {"editor_min_adequacy_rate": 3}, {"max_revision_requests": -1}, {"max_rounds": 3}])
def test_a_malformed_constitution_is_refused(bad):
    with pytest.raises(ValueError):
        constitution.from_mapping(bad)


def test_an_unknown_preset_or_file_is_an_error():
    with pytest.raises(ValueError):
        constitution.load("/no/such/file.yaml")


def test_every_manuscript_records_the_rules_it_was_submitted_under(client):
    j = Journal(client)
    old = j.submit("Submitted under the canonical rules of the journal")
    with constitution.override(reviews_required=2):
        j.add("Late Joiner", 4)
        from tests.helpers import grant
        grant(j.author, 4)
        new = j.submit("Submitted later under a different number of reviewers")
    assert paper(client, old)["constitution_version"] == "MJ-2.2"
    assert paper(client, new)["constitution_version"] == "MJ-2.2+reviews_required=2"
    # And the first paper keeps its three reviewers: the rules never change retrospectively.
    assert paper(client, old)["panel_progress"]["required"] == 3
    assert paper(client, new)["panel_progress"]["required"] == 2


def test_every_logged_event_carries_the_constitution_in_force(client):
    j = Journal(client)
    j.publish()
    events = client.get("/api/events", params={"limit": 1000}).json()["events"]
    assert events and {e["constitution_version"] for e in events} == {"MJ-2.2"}


def test_with_the_quality_gate_off_every_review_counts_and_no_evaluation_is_needed(client):
    with constitution.override(review_quality_gate=False):
        j = Journal(client)
        pid = j.submit()
        j.review_all(pid)
        assert paper(client, pid, j.author)["state"] == "REVIEWS_EVALUATED", "straight to the editor"
        assert all(credits(client, p) >= 5 for p in j.reviewers)
        assert j.decide(pid).status_code == 201


def test_a_different_number_of_required_reviewers_changes_the_price_and_the_panel(client):
    with constitution.override(reviews_required=2):
        j = Journal(client)
        assert credits(client, j.author) == 5
        pid = j.submit()
        assert credits(client, j.author) == 1, "two reviewers plus the editor's two: four credits"
        assert len(j.panel(pid)) == 2


def test_the_constitution_is_public_and_readable_by_machines(client):
    body = client.get("/api/constitution").json()
    assert body["current"]["label"] == "MJ-2.2"
    assert body["current"]["reviews_required"] == 3
    assert body["changelog"]
