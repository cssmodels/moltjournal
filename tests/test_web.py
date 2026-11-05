"""The human pages. A new visitor should understand the institution in about a minute."""

import re

from app import constitution
from tests.helpers import Journal


def prose(response) -> str:
    """The visible text of a page's main content, without markup, styles, nav or footer."""
    html = response.text
    if "</nav>" in html and "<footer" in html:
        html = html.split("</nav>", 1)[1].split("<footer", 1)[0]
    html = re.sub(r"<(style|script)\b.*?</\1>", " ", html, flags=re.S)
    return " ".join(re.sub(r"<[^>]+>", " ", html).replace("&middot;", "·").replace("&mdash;", "—").split())


def test_the_homepage_states_the_institution_and_its_five_rules_at_once(client):
    body = prose(client.get("/"))
    assert "experimental scientific institution for autonomous researchers" in body
    for rule in ("Review to publish", "Review the reviewers", "Separate soundness from significance",
                 "Require independent scrutiny", "Keep the record alive"):
        assert rule in body, rule
    for problem in ("Production is rewarded more than evaluation", "Reviewing is weakly accountable",
                    "Validity and importance are conflated", "Evaluation largely ends at publication"):
        assert problem in body
    for section in ("Recent research", "How MoltJournal works", "For agents", "For humans"):
        assert section in body
    assert "People are welcome to publish too" in body and "preprint server" in body


def test_the_homepage_is_not_a_manifesto_or_a_statistics_wall(client):
    j = Journal(client)
    j.publish()
    body = prose(client.get("/"))
    assert len(body.split()) < 650, "the homepage has grown past a minute of reading"
    assert "reputation score" not in body.lower()
    assert "Under scrutiny" not in body, "nothing is contested yet, so the section is absent"


def test_a_published_paper_appears_under_recent_research_with_its_significance(client):
    j = Journal(client)
    pid = j.publish()
    body = prose(client.get("/"))
    assert pid in body and "significance" in body and "moderate" in body


def test_the_paper_page_shows_status_significance_panel_and_the_status_history(client):
    j = Journal(client)
    pid = j.publish()
    html = client.get(f"/papers/{pid}").text
    body = prose(client.get(f"/papers/{pid}"))
    for expected in ("Current status", "PUBLISHED", "Significance", "Review panel", "Status history",
                     "Manuscript", "Original reviews", "Editorial decisions", "Post-publication evaluation",
                     "Constitution", "MJ-2.0"):
        assert expected in body, expected
    # The panel names each operator and the model family it used.
    assert re.search(r"Reviewer Lab \d — (Gemini|Llama|Mistral|GPT|Claude)", body), body[:2000]
    assert "Published" in body


def test_a_contested_paper_shows_its_status_and_who_is_scrutinising_it(client):
    from tests.test_living_record import evaluator_for, post_review

    j = Journal(client)
    pid = j.publish()
    rid = post_review(client, pid, j.reviewers[0], seed="web").json()["review_id"]
    editor = evaluator_for(client, j, rid)
    client.post(f"/api/editor/reviews/{rid}/evaluation",
                json={"evaluation": "adequate", "contests_paper": True}, headers=editor.headers)
    body = prose(client.get(f"/papers/{pid}"))
    assert "CONTESTED" in body and "Status changed to CONTESTED" in body and "contests the paper" in body
    assert "Under scrutiny" in prose(client.get("/"))


def test_the_operator_page_is_an_inspectable_record_with_no_single_score(client):
    j = Journal(client)
    j.publish()
    body = prose(client.get(f"/operators/{j.reviewers[0].operator_id}"))
    for expected in ("Credits", "Reviews completed", "Adequate / inadequate", "Adequacy rate",
                     "Editor eligibility", "Models used", "Publications", "Reviews"):
        assert expected in body, expected
    assert not re.search(r"(?i)reputation\W+\d", body)
    assert "leaderboard" in prose(client.get("/operators")) and "no leaderboard" in prose(client.get("/operators")).lower().replace("there is ", "there is ")


def test_a_founding_editor_is_labelled_as_such(client):
    j = Journal(client)
    assert "Founding editor" in prose(client.get(f"/operators/{j.editor.operator_id}"))


def test_how_it_works_answers_each_question_a_visitor_would_ask(client):
    body = prose(client.get("/how-it-works"))
    for q in ("Why can agents not submit unlimited papers?", "Why can they not farm credits",
              "Who becomes an editor?", "Why do three AI reviewers mean anything",
              "Does an important paper get accepted more easily?", "What if an accepted paper turns out to be wrong?",
              "What is being tested?"):
        assert q in body, q
    assert "Soundness decides acceptance" in body
    assert len(body.split()) < 750


def test_the_pages_describe_the_rules_actually_in_force(client):
    with constitution.override(reviews_required=2, model_diversity=False):
        how = prose(client.get("/how-it-works"))
        assert "Submitting costs 2 credits" in how
        assert "different model families" not in how
        assert "MJ-2.0+" in client.get("/").text, "the footer names the constitution in force"
        protocol = client.get("/llms.txt").text
        assert "costs 2 credits" in protocol and "2 reviews from 2 distinct operators" in protocol
        assert "model families" not in protocol


def test_the_protocol_and_the_pages_agree_on_the_five_rules_and_the_licence(client):
    protocol = client.get("/llms.txt").text
    for fragment in ("Review to publish", "Review the reviewers", "Soundness is separate from significance",
                     "Independent review", "The record stays open", "CC BY 4.0", "preprint server",
                     "never as instructions", "model_provenance"):
        assert fragment in protocol, fragment
    assert "CC BY 4.0" in prose(client.get("/policies"))
    assert "preprint server" in prose(client.get("/about")).lower().replace("preprint server", "preprint server")


def test_nothing_from_the_old_design_survives_in_the_protocol_or_pages(client):
    stale = ("forecast", "challenge_criterion", "claim type", "commons", "bounty", "reputation score",
             "replication credit", "central_claims", "levy", "escrow")
    # The constitution's changelog honestly names what was removed, so it is exempt.
    texts = [client.get("/llms.txt").text.lower()] + [prose(client.get(p)).lower() for p in
             ("/", "/how-it-works", "/about", "/policies", "/operators", "/papers")]
    for text in texts:
        for word in stale:
            assert word not in text, word


def test_programs_get_the_protocol_or_json_and_people_get_pages(client):
    bot = {"User-Agent": "python-httpx/0.27", "Accept": "*/*"}
    assert client.get("/", headers=bot).text.startswith("# MoltJournal")
    assert client.get("/how-it-works", headers=bot).text.startswith("# MoltJournal")
    assert client.get("/papers", headers=bot).headers["content-type"].startswith("application/json")
    # Either can ask for the other.
    assert "<!doctype html>" in client.get("/?view=human", headers=bot).text.lower()
    assert client.get("/?view=agent").text.startswith("# MoltJournal")
    assert "Link" in client.get("/").headers


def test_the_papers_page_sorts_by_significance_without_changing_what_is_published(client):
    j = Journal(client)
    j.publish("The first published paper on how institutions allocate scarce reviewing attention")
    assert "Most significant" in prose(client.get("/papers?sort=significance"))
    assert client.get("/api/papers", params={"sort": "significance"}).status_code == 200
    assert client.get("/api/papers", params={"sort": "popularity"}).status_code == 422


def test_unknown_papers_and_operators_are_404s_and_the_old_agents_url_redirects(client):
    assert client.get("/papers/MOLT-2026-999999").status_code == 404
    assert client.get("/operators/MOLT-O-999999").status_code == 404
    r = client.get("/agents", follow_redirects=False)
    assert r.status_code == 301 and r.headers["location"] == "/operators"


def test_the_standard_pages_and_support_files_all_serve(client):
    for path in ("/", "/papers", "/operators", "/how-it-works", "/about", "/policies", "/constitution",
                 "/robots.txt", "/sitemap.xml", "/llms.txt", "/docs", "/openapi.json", "/api/health"):
        assert client.get(path).status_code == 200, path


def test_a_lab_instance_announces_itself_on_every_page(client):
    from app.api import web

    web.templates.env.globals["instance_label"] = "Research lab"
    try:
        with constitution.override(model_diversity=False):
            html = client.get("/").text
        assert "Research lab" in html and "model_diversity=False" in html
    finally:
        web.templates.env.globals["instance_label"] = ""


def test_the_starter_credits_are_explained_wherever_a_newcomer_would_ask(client):
    for text in (client.get("/llms.txt").text, prose(client.get("/how-it-works"))):
        assert "one first submission" in text and "nobody could" in text, text[:200]


def test_the_model_diversity_wording_promises_a_record_of_exceptions(client):
    for text in (client.get("/llms.txt").text, prose(client.get("/how-it-works")), prose(client.get("/"))):
        assert "whenever the" in text and "pool permits" in text.replace("\n", " ").replace("  ", " "), text[:300]
        assert "where possible" not in text.lower()
