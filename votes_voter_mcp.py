"""FastMCP server for the PrestoVotes WhatsApp number: the public vote, nominate, check their votes
and complain. Q (Presto's WhatsApp AI) is the only client.

Who is messaging comes from Q, not from the conversation: every request carries X-Q-Context, a JWT
Q signs (ES256) with the sender's WhatsApp number, which Meta has verified. This server checks it
against Q's published key (Q_JWKS_URL) and rejects anything else, so it can't be used outside Q
and a voter can't vote, or read votes, as someone else by typing a different number.

It calls the PrestoVotes voter API (/api/v1/voter) with a platform key made by
`flask mcp-key create --platform --label voter-mcp`, passing the verified phone in X-Voter-Phone.

Run:
    PRESTO_VOTES_URL=https://prestovotes.com PRESTO_VOTES_VOTER_KEY=pvk_... \\
    Q_JWKS_URL=https://q.prestoghana.com/.well-known/jwks.json \\
    VOTER_MCP_PUBLIC_URL=https://voters.mcp.prestovotes.com/mcp \\
    uv run votes_voter_mcp.py
"""

import json
import os
from typing import Any

import httpx
import jwt
import uvicorn
from dotenv import load_dotenv
from fastmcp import FastMCP
from fastmcp.server.dependencies import get_http_headers
from mcp.types import ToolAnnotations

load_dotenv()

PRESTO_VOTES_URL = os.getenv("PRESTO_VOTES_URL", "https://prestovotes.com").strip().rstrip("/")
VOTER_API_URL = f"{PRESTO_VOTES_URL}/api/v1/voter"
VOTER_KEY = os.getenv("PRESTO_VOTES_VOTER_KEY", "").strip()
Q_JWKS_URL = os.getenv("Q_JWKS_URL", "https://q.prestoghana.com/.well-known/jwks.json").strip()
Q_ISSUER = os.getenv("Q_ISSUER", "https://q.prestoghana.com").strip().rstrip("/")
# The URL Q is configured with for this server; Q signs it as the JWT audience.
PUBLIC_URL = os.getenv("VOTER_MCP_PUBLIC_URL", "http://localhost:8012/mcp").strip()
CONTEXT_HEADER = "x-q-context"
REQUEST_TIMEOUT_SECONDS = 20

READ = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)
WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True)

INSTRUCTIONS = """
You are PrestoVotes on WhatsApp. PrestoVotes runs paid public voting for awards and pageants in
Ghana: people pay per vote by mobile money. Be brief and warm; this is a chat on a phone.

Voting: find the event (find_events), then the nominee (find_nominees; match names loosely and ask
if more than one fits). Before calling start_vote, always confirm in one message: the nominee, the
event, the number of votes, the total in GHS (votes x cost per vote) and their network (MTN, Telecel
or AirtelTigo; CARD sends a payment link instead). Only call start_vote after they say yes. Then tell
them to approve the mobile money prompt on their phone. Votes count once the payment succeeds.

Their phone number is already known and verified; never ask for it to vote. Never vote for someone
without a clear yes. If start_vote says there are payments waiting, ask them to approve or cancel
those first.

"Did my vote go through?": use check_my_votes. Paid means counted; pending means waiting for them to
approve the prompt; failed means nothing was charged, and they can try again.

Nominating: the event must be open for nominations. Collect the nominee's name, category, phone
number and a photo (they can send it here; it arrives as "Image Url: ..."), then call nominate.

Complaints, refunds or anything you can't solve: file_complaint with the details and any payment
ref from check_my_votes, and tell them the team will follow up. Don't promise refunds.
""".strip()

_jwks = jwt.PyJWKClient(Q_JWKS_URL, cache_keys=True, lifespan=3600)


class ContextError(Exception):
    pass


def verify_context(token: str | None) -> dict[str, Any]:
    if not token:
        raise ContextError("missing X-Q-Context")
    try:
        key = _jwks.get_signing_key_from_jwt(token).key
        return jwt.decode(token, key, algorithms=["ES256"], audience=PUBLIC_URL, issuer=Q_ISSUER,
                          options={"require": ["exp", "iat", "aud", "iss"]})
    except (jwt.PyJWTError, jwt.PyJWKClientError) as exc:
        raise ContextError(str(exc)) from exc


def require_q_context(asgi_app):
    """Rejects every HTTP request that doesn't carry a valid X-Q-Context from Q."""
    async def gated(scope, receive, send):
        if scope.get("type") != "http":
            await asgi_app(scope, receive, send)
            return
        token = next((v.decode() for k, v in scope.get("headers", []) if k.lower() == CONTEXT_HEADER.encode()), None)
        try:
            verify_context(token)
        except ContextError as exc:
            body = json.dumps({"error": "unauthorized", "detail": str(exc)}).encode()
            await send({"type": "http.response.start", "status": 401,
                        "headers": [(b"content-type", b"application/json")]})
            await send({"type": "http.response.body", "body": body})
            return
        await asgi_app(scope, receive, send)
    return gated


def _voter_phone() -> str | None:
    """The sender's verified phone, from the context Q signed for this request."""
    claims = verify_context(get_http_headers(include={CONTEXT_HEADER}).get(CONTEXT_HEADER))
    user = claims.get("user") or {}
    return user.get("phone") if user.get("phone_verified") else None


async def _api(method: str, path: str, *, params: dict | None = None, body: dict | None = None) -> dict[str, Any]:
    headers = {"x-api-key": VOTER_KEY, "Accept": "application/json"}
    try:
        phone = _voter_phone()
    except ContextError:
        return {"success": False, "error": "This conversation couldn't be verified."}
    if phone:
        headers["X-Voter-Phone"] = phone
    clean = {k: v for k, v in (params or {}).items() if v not in (None, "")}
    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS) as client:
            response = await client.request(method, f"{VOTER_API_URL}{path}", params=clean, json=body, headers=headers)
    except httpx.HTTPError:
        return {"success": False, "error": "PrestoVotes isn't reachable right now. Please try again shortly."}
    try:
        payload = response.json()
    except ValueError:
        payload = {}
    if response.status_code >= 400:
        return {"success": False, "error": payload.get("error") or f"Something went wrong (HTTP {response.status_code})."}
    result = {"success": True, "data": payload.get("data")}
    if "meta" in payload:
        result["meta"] = {k: payload["meta"][k] for k in ("total", "page", "pages") if k in payload["meta"]}
    return result


mcp = FastMCP("PrestoVotes", instructions=INSTRUCTIONS)


@mcp.tool(annotations=READ)
async def find_events(q: str | None = None) -> dict[str, Any]:
    """
    Events open for voting or nominations, with whether each is taking votes or nominations and its
    cost per vote in GHS.

    Args:
        q: part of the event's name, e.g. "GPSA"
    """
    return await _api("GET", "/events", params={"q": q, "pageSize": 20})


@mcp.tool(annotations=READ)
async def get_event(slug: str) -> dict[str, Any]:
    """
    An open event's categories, cost per vote and closing date.

    Args:
        slug: the event's slug from find_events
    """
    return await _api("GET", f"/events/{slug}")


@mcp.tool(annotations=READ)
async def find_nominees(slug: str, q: str | None = None, category: str | None = None, page: int = 1) -> dict[str, Any]:
    """
    Approved nominees in an event, with their id (needed for start_vote), code and category. Vote
    counts are included unless the organisers have hidden results (then votes is null; don't guess).

    Args:
        slug: the event's slug
        q: part of the nominee's name, or their exact code
        category: a category title from get_event
        page: page number, from 1
    """
    return await _api("GET", f"/events/{slug}/nominees", params={"q": q, "category": category, "page": page, "pageSize": 20})


@mcp.tool(annotations=WRITE)
async def start_vote(nominee_id: int, votes: int, network: str, note: str | None = None) -> dict[str, Any]:
    """
    Buy votes for a nominee: sends a mobile money prompt to the voter's own WhatsApp number (or, with
    network CARD, returns a payment link). Only call after the voter has confirmed the nominee, number
    of votes, total and network.

    Args:
        nominee_id: the nominee's id from find_nominees
        votes: how many votes, 1-10000
        network: MTN, TELECEL, AIRTELTIGO, or CARD for a card/payment link
        note: optional message from the voter to the nominee
    """
    return await _api("POST", "/votes", body={"nominee_id": nominee_id, "votes": votes, "network": network, "note": note})


@mcp.tool(annotations=READ)
async def check_my_votes() -> dict[str, Any]:
    """
    The voter's recent vote payments: nominee, event, votes, amount, and status (paid = counted,
    pending = waiting for them to approve the prompt, failed = not charged).
    """
    return await _api("GET", "/votes/mine")


@mcp.tool(annotations=WRITE)
async def nominate(slug: str, category: str, name: str, nominee_phone: str, photo_url: str,
                   description: str | None = None, institution: str | None = None) -> dict[str, Any]:
    """
    Nominate someone in an event that's open for nominations. The nominee gets an SMS and the
    organisers review it before it appears.

    Args:
        slug: the event's slug
        category: a category title from get_event
        name: the nominee's full name
        nominee_phone: the nominee's phone number
        photo_url: the photo the voter sent (the URL after "Image Url:")
        description: optional short reason or bio
        institution: optional school, company or group
    """
    return await _api("POST", "/nominations", body={
        "slug": slug, "category": category, "name": name, "nominee_phone": nominee_phone, "photo_url": photo_url,
        "description": description, "institution": institution})


@mcp.tool(annotations=WRITE)
async def file_complaint(message: str, ref: str | None = None, event: str | None = None) -> dict[str, Any]:
    """
    Log a complaint or request for the PrestoVotes team (e.g. paid but votes didn't count, refund,
    wrong nominee). Include what happened in the voter's words.

    Args:
        message: what went wrong
        ref: payment ref from check_my_votes, if it's about a payment
        event: the event's name or slug, if relevant
    """
    return await _api("POST", "/complaints", body={"message": message, "ref": ref, "event": event})


@mcp.resource("q://context", name="voter_context", mime_type="text/plain",
              description="This voter's recent payments, given to the assistant before each turn.")
async def voter_context() -> str:
    result = await _api("GET", "/votes/mine", params={"limit": 5})
    if not result.get("success"):
        return "Couldn't load this voter's payments."
    rows = result.get("data") or []
    if not rows:
        return "This voter hasn't voted on PrestoVotes from this number before."
    lines = [f"- {r['createdAt'][:16].replace('T', ' ')} UTC: {r['votes']} votes for {r['nominee']} ({r['event']}), "
             f"GHS {r['amount']:.2f}, {r['status']}" + (f", ref {r['ref']}" if r.get("ref") else "") for r in rows]
    pending = sum(1 for r in rows if r["status"] == "pending")
    head = f"This voter's last {len(rows)} payments" + (f" ({pending} waiting for approval)" if pending else "") + ":"
    return "\n".join([head, *lines])


app = require_q_context(mcp.http_app(path="/mcp"))

if __name__ == "__main__":
    uvicorn.run(app, host=os.getenv("HOST", "0.0.0.0"), port=int(os.getenv("PORT", "8012")))
