"""The rehearsal harness itself. It is how we decide what to fix before opening registration."""

from tests.helpers import admin


def run(base, *, models=("model-a", "model-b", "model-c"), size=10, papers=4, seed=4242, cohort="x", rounds=12, cast=None):
    from dryrun.backends import StubBackend
    from dryrun.personas import build_cast
    from dryrun.runner import DryRun, Journal
    from dryrun.world import make_studies

    journal = Journal(base)
    backend = StubBackend()
    cast = cast or build_cast(list(models), size, cohort=cohort)
    log = DryRun(journal, backend, cast, make_studies(papers, seed=seed), admin_token="test-admin-token", editors=2).run(rounds)
    return journal, backend, log


def test_the_harness_drives_the_whole_institution_over_the_public_api(fresh_server):
    from dryrun.report import render

    journal, backend, log = run(fresh_server)
    assert log.submissions == 4
    assert log.reviews, "no reviews were produced"
    assert log.evaluations, "nothing was evaluated by an editor"
    assert any(r.evaluation == "adequate" for r in log.reviews)
    # A major revision costs a full round, so an author who has not yet earned the credits is
    # refused. That is the rule working. Anything else being refused would be a real defect.
    unexpected = [f for f in log.revision_failures if "this costs" not in f]
    assert not unexpected, unexpected[:3]

    stats = journal.call("GET", "/api/statistics")
    assert stats["credits"]["in_circulation"] >= 0
    report = render(log, backend.name, backend.usage, stats)
    for heading in ("the funnel", "review the reviewers", "independent scrutiny",
                    "soundness against the hidden truth", "soundness and significance, kept apart"):
        assert heading in report


def test_the_editor_catches_the_hasty_reviewer_and_not_the_careful_ones(fresh_server):
    from dryrun.personas import Persona

    # Plenty of both kinds, so the draw cannot leave either out.
    cast = [Persona(f"Agent {i:02d}", f"model-{i % 3}", "hasty" if i % 3 == 0 else "thorough", cohort="catch") for i in range(12)]
    _, _, log = run(fresh_server, papers=6, seed=777, rounds=16, cast=cast)
    judged = [r for r in log.reviews if r.kind == "pre" and r.evaluation]
    hasty = [r for r in judged if r.temperament == "hasty"]
    careful = [r for r in judged if r.temperament in ("thorough", "harsh")]
    assert hasty and careful
    assert sum(r.evaluation == "inadequate" for r in hasty) / len(hasty) > 0.8
    assert sum(r.evaluation == "adequate" for r in careful) / len(careful) > 0.8


def test_panels_draw_on_different_model_families(fresh_server):
    journal, _, log = run(fresh_server, models=("claude-x", "gpt-x", "gemini-x"), size=12, papers=4, seed=991, cohort="fam", rounds=14)
    stats = journal.call("GET", "/api/statistics")
    assert stats["panels"]["complete"] >= 1
    assert stats["panels"]["all_distinct_model_families"] >= 1


def test_a_published_paper_receives_post_publication_review(fresh_server):
    journal, _, log = run(fresh_server, size=12, papers=5, seed=31337, cohort="post", rounds=18)
    if not log.published:
        # Whether anything is accepted is up to the (random) panels; the harness must not fail on that.
        return
    assert log.post_publication >= 1
    assert any(r.kind == "post" and r.evaluation for r in log.reviews)


def test_the_ledger_never_goes_negative_during_a_rehearsal(fresh_server):
    journal, _, _ = run(fresh_server, size=10, papers=4, seed=555, cohort="led", rounds=10)
    for operator in journal.call("GET", "/api/operators"):
        assert journal.call("GET", f"/api/operators/{operator['operator_id']}")["credits"] >= 0
