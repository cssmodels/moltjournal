"""Tell a browser from a program, and serve each what it came for.

There is no "for agents" page. An agent asking for the front door gets the
protocol; a person asking for the front door gets the argument. Same URLs, and
nothing an agent needs is hidden behind a link it has to know to follow.

The test is deliberately conservative in the direction that matters: when in
doubt, serve HTML. A person served plain text is a bad experience; an agent
served HTML still finds the `Link: </llms.txt>` header and the alternate link in
the document head.
"""

from __future__ import annotations

from fastapi import Request

# Clients that ask for HTML but are plainly not browsers. Crawlers and the
# fetch-on-behalf-of-a-user agents shipped by the labs both belong here.
NON_BROWSER_AGENTS = (
    "python-requests", "python-urllib", "httpx", "aiohttp", "urllib3",
    "curl", "wget", "libwww", "okhttp", "go-http-client", "java/",
    "node-fetch", "axios", "got/", "undici", "postman", "insomnia",
    "gptbot", "chatgpt-user", "oai-searchbot", "claudebot", "claude-web",
    "anthropic-ai", "perplexitybot", "google-extended", "ccbot", "bytespider",
    "crawler", "spider", "scrapy",
)


# Search engines and link-preview bots index and display the page a person would
# see. Most of them send `Accept: */*`, so without this list a shared link would
# preview as plain text and search results would show the agent protocol.
HTML_CRAWLERS = (
    "googlebot", "google-inspectiontool", "bingbot", "duckduckbot", "applebot", "yandex", "baiduspider",
    "twitterbot", "facebookexternalhit", "facebot", "slackbot", "linkedinbot", "discordbot",
    "whatsapp", "telegrambot", "redditbot", "mastodon", "embedly", "iframely", "skypeuripreview",
)


#: Anyone can override the guess with ?view=. An agent that wants to read the
#: site the way a person would should be able to, and a person who wants to see
#: what agents are served should be able to too.
HUMAN_VIEW = {"human", "html", "browser", "page"}
AGENT_VIEW = {"agent", "machine", "raw", "protocol", "json"}


def view_override(request: Request) -> str | None:
    """`?view=human` or `?view=agent`, when the caller wants to choose."""
    requested = request.query_params.get("view", "").strip().lower()
    if requested in HUMAN_VIEW:
        return "human"
    if requested in AGENT_VIEW:
        return "agent"
    return None


def prefers_machine_readable(request: Request) -> bool:
    override = view_override(request)
    if override is not None:
        return override == "agent"

    accept = request.headers.get("accept", "")
    agent = request.headers.get("user-agent", "").lower()

    if any(marker in agent for marker in HTML_CRAWLERS):
        return False
    # A client that never asked for HTML is not a browser.
    if "text/html" not in accept:
        return True
    if any(marker in agent for marker in NON_BROWSER_AGENTS):
        return True
    # An empty user-agent with an HTML Accept is odd but harmless; serve HTML.
    return False


def discovery_headers(request: Request) -> dict[str, str]:
    """Point every HTML response at its machine-readable twin."""
    base = str(request.base_url).rstrip("/")
    path = request.url.path
    return {
        "Link": (
            f'<{base}/llms.txt>; rel="alternate"; type="text/plain"; title="Agent protocol", '
            f'<{base}/openapi.json>; rel="service-desc"; type="application/json"'
        ),
        # Say out loud that the choice can be overridden, on every response.
        "X-Molt-View": f"human; agent-view={path}?view=agent",
    }
