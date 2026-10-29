"""FastMCP server for PrestoVotes admins (read-only).

Wraps the PrestoVotes MCP data API (/api/v1/mcp on prestovotes.com). Every request needs a bearer
token, which PrestoVotes issues when someone connects Claude from prestovotes.com/connect-claude
(OAuth: they sign in and click Allow). Keys from `flask mcp-key create` work as bearer tokens too.
What the token can see is decided by PrestoVotes:

- Presto staff see every event, plus platform overview, payment search and organisers
- an organiser sees only their own event; voter phone numbers come back masked

Unauthenticated requests get a 401 pointing at /.well-known/oauth-protected-resource, which names
PrestoVotes as the authorization server, so Claude can start sign-in on its own.

Run:
    MCP_PUBLIC_URL=https://mcp.prestovotes.com \
    PRESTO_VOTES_URL=https://prestovotes.com \
    uv run votes_mcp.py
"""

import os
import time
from typing import Any

import httpx
from dotenv import load_dotenv
from fastmcp import FastMCP
from fastmcp.server.auth import AccessToken, RemoteAuthProvider, TokenVerifier
from fastmcp.server.dependencies import get_access_token
from mcp.types import ToolAnnotations

load_dotenv()

SERVER_NAME = "PrestoVotes"
PRESTO_VOTES_URL = os.getenv("PRESTO_VOTES_URL", "https://prestovotes.com").strip().rstrip("/")
DATA_API_URL = os.getenv("PRESTO_VOTES_MCP_API_URL", f"{PRESTO_VOTES_URL}/api/v1/mcp").strip().rstrip("/")
MCP_PUBLIC_URL = os.getenv("MCP_PUBLIC_URL", "http://localhost:8011").strip().rstrip("/")
REQUEST_TIMEOUT_SECONDS = 30
# How long a verified token is trusted before PrestoVotes is asked again (so revocation lands within this).
TOKEN_CACHE_SECONDS = 60

READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True)

INSTRUCTIONS = """
Read-only access to PrestoVotes, a Ghanaian voting platform for awards and pageants where the
public pay per vote by mobile money (USSD or web). An "event" is an award; its slug identifies it.
Nominees (candidates) sit in categories; each paid transaction buys votes for one nominee.
Amounts are in GHS. "organiserShare" is the organiser's cut of revenue (organiserShareRate).

Start with whoami to see the connection's scope. Presto staff ("platform") can use every tool; an
organiser ("award") can only read their own event, so pass that event's slug, and
get_platform_overview, search_transactions and list_organisers will refuse them. Voter phone
numbers are masked for organisers. Nothing here can change data.
""".strip()


class VotesAPIError(Exception):
    """Raised when the PrestoVotes MCP data API request fails."""


class VotesClient:
    def __init__(self, base_url: str) -> None:
        self.base_url = base_url

    async def get(self, token: str, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        clean = {k: (str(v).lower() if isinstance(v, bool) else v) for k, v in (params or {}).items() if v is not None}

        try:
            async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS) as client:
                response = await client.get(
                    f"{self.base_url}{path}",
                    params=clean,
                    headers={"Accept": "application/json", "Authorization": f"Bearer {token}"},
                )
        except httpx.TimeoutException as exc:
            raise VotesAPIError("The PrestoVotes API request timed out.") from exc
        except httpx.RequestError as exc:
            raise VotesAPIError(f"Could not connect to the PrestoVotes API: {exc}") from exc

        try:
            body = response.json()
        except ValueError:
            body = {"error": response.text[:500]}

        if response.status_code in (401, 403):
            raise VotesAPIError(body.get("error") or f"Not authorised (HTTP {response.status_code}).")
        if response.status_code == 404:
            raise VotesAPIError(body.get("error") or "Not found (HTTP 404). Organisers can only see their own event.")
        if response.status_code >= 400:
            raise VotesAPIError(f"PrestoVotes API returned HTTP {response.status_code}: {body.get('error') or body}")
        return body


class PrestoVotesTokenVerifier(TokenVerifier):
    """Checks bearer tokens by asking PrestoVotes who they belong to (GET /me)."""

    def __init__(self, client: VotesClient) -> None:
        super().__init__()
        self.client = client
        self._cache: dict[str, tuple[float, AccessToken]] = {}

    async def verify_token(self, token: str) -> AccessToken | None:
        cached = self._cache.get(token)
        if cached and time.monotonic() - cached[0] < TOKEN_CACHE_SECONDS:
            return cached[1]
        try:
            me = (await self.client.get(token, "/me")).get("data") or {}
        except VotesAPIError:
            self._cache.pop(token, None)
            return None
        award = (me.get("award") or {}).get("slug")
        access = AccessToken(token=token, client_id=me.get("label") or "prestovotes", scopes=["votes:read"],
                             claims={"scope": me.get("scope"), "award": award})
        if len(self._cache) > 1000:
            self._cache.clear()
        self._cache[token] = (time.monotonic(), access)
        return access


async def _call(path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    access = get_access_token()
    if access is None:
        return {"success": False, "error": "Not signed in to PrestoVotes. Reconnect the connector."}
    try:
        body = await votes_client.get(access.token, path, params)
    except VotesAPIError as exc:
        return {"success": False, "error": str(exc)}
    result = {"success": True, "data": body.get("data")}
    if "meta" in body:
        result["meta"] = body["meta"]
    return result


votes_client = VotesClient(DATA_API_URL)
auth = RemoteAuthProvider(
    token_verifier=PrestoVotesTokenVerifier(votes_client),
    authorization_servers=[PRESTO_VOTES_URL],
    base_url=MCP_PUBLIC_URL,
    scopes_supported=["votes:read"],
    resource_name="PrestoVotes",
)
mcp = FastMCP(SERVER_NAME, instructions=INSTRUCTIONS, auth=auth)


@mcp.tool(annotations=READ_ONLY)
async def whoami() -> dict[str, Any]:
    """
    Show the connection's scope: "platform" (Presto staff, every event) or "award" (one event,
    returned with its slug). Call this first.
    """
    return await _call("/me")


@mcp.tool(annotations=READ_ONLY)
async def get_platform_overview(days: int = 30) -> dict[str, Any]:
    """
    Staff only. Platform-wide revenue, votes, payments and payment success rate for the last `days`
    days, with the previous period and % change, a daily series, USSD vs web votes, top 10 events
    by revenue, live events, events taking nominations, nominees awaiting approval and organiser count.
    """
    return await _call("/overview", {"days": days})


@mcp.tool(annotations=READ_ONLY)
async def list_events(
    q: str | None = None,
    voting_open: bool | None = None,
    nominations_open: bool | None = None,
    sort: str = "newest",
    page: int = 1,
    page_size: int = 50,
) -> dict[str, Any]:
    """
    List events (awards) with all-time votes, nominee count and paid revenue. Organiser keys only
    get their own event.

    Args:
        q: search event name, slug or organiser username
        voting_open: only events with voting open (true) or closed (false)
        nominations_open: only events taking nominations (true) or not (false)
        sort: newest, revenue, votes or name
        page: page number, from 1
        page_size: 1-200
    """
    return await _call("/awards", {"q": q, "votingOpen": voting_open, "nominationsOpen": nominations_open,
                                        "sort": sort, "page": page, "pageSize": page_size})


@mcp.tool(annotations=READ_ONLY)
async def get_event(slug: str, days: int = 30) -> dict[str, Any]:
    """
    One event in detail: settings (voting/nominations open, results hidden, cost per vote, dates),
    all-time votes, revenue, payments, organiser share and votes by channel, counts of categories,
    approved and pending nominees and contacts, and the last `days` days vs the period before with a
    daily series.

    Args:
        slug: the event's slug, e.g. from list_events or whoami
        days: window for the recent-performance figures (1-366)
    """
    return await _call(f"/awards/{slug}", {"days": days})


@mcp.tool(annotations=READ_ONLY)
async def get_categories(slug: str) -> dict[str, Any]:
    """
    An event's categories with nominee count, total votes and the current leader of each.

    Args:
        slug: the event's slug
    """
    return await _call(f"/awards/{slug}/categories")


@mcp.tool(annotations=READ_ONLY)
async def get_nominees(
    slug: str,
    category: str | None = None,
    q: str | None = None,
    approved: bool | None = None,
    sort: str = "votes",
    page: int = 1,
    page_size: int = 50,
) -> dict[str, Any]:
    """
    An event's nominees with votes, category, code and approval status. Sorted by votes this is the
    leaderboard; filter by category for a category's standings.

    Args:
        slug: the event's slug
        category: exact category title, as returned by get_categories
        q: search nominee name or code
        approved: true for approved nominees, false for those awaiting approval
        sort: votes, name or newest
        page: page number, from 1
        page_size: 1-200
    """
    return await _call(f"/awards/{slug}/nominees", {"category": category, "q": q, "approved": approved,
                                                         "sort": sort, "page": page, "pageSize": page_size})


@mcp.tool(annotations=READ_ONLY)
async def get_event_transactions(
    slug: str,
    paid: bool | None = None,
    candidate_id: str | None = None,
    channel: str | None = None,
    since: str | None = None,
    until: str | None = None,
    page: int = 1,
    page_size: int = 50,
) -> dict[str, Any]:
    """
    An event's vote payments, newest first: amount, votes, nominee, paid/pending, channel, network,
    payment ref, voter phone and vote message.

    Args:
        slug: the event's slug
        paid: true for successful payments, false for failed or unfinished ones
        candidate_id: only payments for this nominee id
        channel: ussd or web
        since: ISO date or datetime (UTC), inclusive
        until: ISO date or datetime (UTC), exclusive
        page: page number, from 1
        page_size: 1-200
    """
    return await _call(f"/awards/{slug}/transactions", {
        "paid": paid, "candidateId": candidate_id, "channel": channel, "since": since, "until": until,
        "page": page, "pageSize": page_size})


@mcp.tool(annotations=READ_ONLY)
async def search_transactions(
    ref: str | None = None,
    account: str | None = None,
    award: str | None = None,
    paid: bool | None = None,
    since: str | None = None,
    until: str | None = None,
    page: int = 1,
    page_size: int = 50,
) -> dict[str, Any]:
    """
    Staff only. Search payments across every event, newest first, e.g. to trace a voter's complaint
    that their votes didn't count.

    Args:
        ref: exact payment reference
        account: voter phone number, or part of it
        award: limit to one event slug
        paid: true for successful payments, false for failed or unfinished ones
        since: ISO date or datetime (UTC), inclusive
        until: ISO date or datetime (UTC), exclusive
        page: page number, from 1
        page_size: 1-200
    """
    return await _call("/transactions", {"ref": ref, "account": account, "award": award, "paid": paid,
                                              "since": since, "until": until, "page": page, "pageSize": page_size})


@mcp.tool(annotations=READ_ONLY)
async def list_organisers(q: str | None = None, page: int = 1, page_size: int = 50) -> dict[str, Any]:
    """
    Staff only. Organiser accounts, newest first, with contact details and their event.

    Args:
        q: search username, email, phone or event name
        page: page number, from 1
        page_size: 1-200
    """
    return await _call("/organisers", {"q": q, "page": page, "pageSize": page_size})


if __name__ == "__main__":
    mcp.run(transport="http", host=os.getenv("HOST", "0.0.0.0"), port=int(os.getenv("PORT", "8011")))
