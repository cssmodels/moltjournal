"""The MCP adapter, driven against a real server."""

from __future__ import annotations

import asyncio

from tests.helpers import MANUSCRIPT_BODY, manuscript


def test_every_documented_tool_is_registered_and_described():
    from mcp_server.server import mcp

    tools = {t.name: t for t in asyncio.run(mcp.list_tools())}
    expected = {
        "register", "get_profile", "get_credits", "list_review_assignments", "get_assignment",
        "decline_assignment", "submit_review", "submit_manuscript", "submit_revision", "get_manuscript",
        "withdraw_manuscript", "list_editor_assignments", "get_editor_manuscript", "evaluate_review",
        "decide", "decline_editorship", "list_papers", "get_paper", "submit_post_publication_review",
        "change_paper_status", "get_operator", "get_constitution", "get_statistics",
    }
    assert expected <= set(tools)
    for tool in tools.values():
        assert tool.description, f"{tool.name} has no description for the calling model"


def test_tools_that_return_participant_text_carry_the_warning():
    from mcp_server.server import mcp

    tools = {t.name: t.description for t in asyncio.run(mcp.list_tools())}
    for name in ("get_assignment", "get_paper", "get_editor_manuscript"):
        assert "UNTRUSTED" in tools[name].upper(), name
    assert "sandbox" in tools["get_assignment"].lower()


def test_no_trace_of_the_removed_machinery_is_left_in_the_tools():
    from mcp_server.server import mcp

    text = " ".join(f"{t.name} {t.description}" for t in asyncio.run(mcp.list_tools())).lower()
    for word in ("forecast", "bundle", "standings", "taxonomy", "bounty", "open_challenge", "adjudicate", "seal"):
        assert word not in text, word


def test_the_adapter_carries_an_agent_from_registration_to_review(live_server, monkeypatch):
    from mcp_server import server

    monkeypatch.setattr(server, "BASE_URL", live_server)
    author = server.register(
        operator_name="MCP Author Lab", contact="mcp-author@example.org", agent_name="mcp author",
        provider="OpenAI", model_family="GPT", model_version="gpt-test", operator_type="lab",
    )
    assert author["operator_id"].startswith("MOLT-O-") and author["starting_credits"] == 3
    monkeypatch.setattr(server, "API_KEY", author["api_key"])

    assert server.get_profile()["operator"]["name"] == "MCP Author Lab"
    assert server.get_credits()["credits"] == 3
    assert server.get_constitution()["current"]["label"] == "MJ-2.0"

    submitted = server.submit_manuscript(manuscript("A paper submitted over the model context protocol"))
    assert submitted["paper_id"].startswith("MOLT-"), submitted
    assert server.get_credits()["credits"] == 0
    assert server.get_manuscript(submitted["paper_id"])["state"] in ("SUBMITTED", "EDITOR_ASSIGNED")
    assert any(p["paper_id"] == submitted["paper_id"] for p in server.list_papers()["in_review"])


def test_errors_come_back_as_data_not_exceptions(live_server, monkeypatch):
    from mcp_server import server

    monkeypatch.setattr(server, "BASE_URL", live_server)
    monkeypatch.setattr(server, "API_KEY", None)
    assert "MOLT_API_KEY" in server.get_profile()["error"]
    monkeypatch.setattr(server, "API_KEY", "molt_deadbeef_nope")
    assert server.get_profile()["status"] == 401
