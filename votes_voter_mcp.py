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
import time
from datetime import datetime
from typing import Any
from urllib.parse import quote

import httpx
import jwt
import uvicorn
from dotenv import load_dotenv
from fastmcp import FastMCP
from fastmcp.server.dependencies import get_http_headers
from mcp.types import ToolAnnotations

from mcp_logs import RequestLogging, ToolLogging, log_api_call, mask_phone, setup

load_dotenv()
log = setup("votes_voter_mcp")

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

MY_NOMINATIONS_URL = f"{PRESTO_VOTES_URL}/my-nominations"

INSTRUCTIONS = f"""
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

Links: only give a link that a tool returned or that is written out in full in the context. Never
build or guess a PrestoVotes link yourself, even if you can see a pattern.

Voting codes: "What's my code?" from a nominee: it's in the live context once they're approved; if
they aren't approved yet, say the organisers are still reviewing it. For anyone else's code or voting
link, use find_nominees, which returns both. Anyone can dial the code to vote.

Flyers: a flyer here always means a nominee's PrestoVotes flyer, which PrestoVotes draws automatically
from the nomination (name, photo, category, event, voting code) and send_flyer delivers into this chat
as a picture. You don't design flyers: never ask what should go on one, never describe what one will
contain, and never say you'll create one.
- Their own flyer: the nominee ids are in the context under what this number has been nominated for.
  Call send_flyer straight away, no questions first. If they have several nominations and ask for
  "my flyers" or "all", send every one (one send_flyer call each); if they ask for one flyer and have
  several, list the categories in one line and ask which.
- Someone else's flyer: find the nominee with find_nominees first, then send_flyer.
- If they ask for "my flyer" and the context shows no nomination for this number, say you can't find
  one for them and ask who the flyer is for.
- After sending, reply in one short line ("Here are your 5 flyers 👆", "Sent!"). For their own
  flyers, add that they can change the photo or details at {MY_NOMINATIONS_URL}. If send_flyer
  gives a flyerUrl instead of sending, share that link exactly. Never write a flyer link yourself.
Offer flyers to nominees when it would help them campaign.

Questions about an event (what it is, dates, venue, how winners are chosen, tickets, prizes): call
get_event and answer from its about and faq in your own short words. If the answer isn't there, say
you don't know yet and offer to ask the organisers; if they'd like that, call ask_organisers with the
event's slug and their question in a clear sentence, and tell them the organisers' answer will come
to this chat. When the context shows an organiser has answered one of their questions, use that
answer. Never make up dates, venues, prizes or rules.

Changing a nomination: when nominees want to change anything about their own nomination (photo,
name, category, the WhatsApp number on it), send them to {MY_NOMINATIONS_URL}. They sign in there
with a code sent to the number they were nominated with and see all their nominations. While
nominations are open, changes save straight away and they get the updated flyer; once voting has
started, their changes go to the organisers for review. You can't change nominations yourself, so
don't collect the new details here. If someone wants to change a nomination that isn't theirs, the
nominee has to do it, or they can ask the organisers.

"I was nominated but didn't get a message": if the context shows a nomination for this number, it's
theirs: confirm it from the context (event, category, and either still under review or approved with
its voting code), offer their flyer, and mention {MY_NOMINATIONS_URL} for changes. If the context
shows none, the nomination was most likely made with a different phone number, or it's still under
review (find_nominees only shows approved nominees). Ask for the name they were nominated under and
the event, then search with find_nominees.
- Found: share the voting code, voting link and flyer as you would for anyone, and explain that the
  nomination has a different number, so our messages went there and they can't sign in at
  {MY_NOMINATIONS_URL} with this one.
- Not found: say you can't see it from here, as it may still be under review or under another number.
  Don't guess, and don't promise it exists.
Either way, offer to ask the organisers to check it and put this number on it. If they'd like that,
call ask_organisers with the event's slug and one clear sentence giving the name, the category if
they know it, and this number written out (the organisers don't see it otherwise), e.g. "Ama Serwaa
says she was nominated for Best Dressed but didn't get our message; please check her nomination and
update its number to 0244123456." Tell them the organisers' answer will come to this chat.

Nominee notes: when a nominee (or a voter) tells you something useful about a nominee, save it with
save_nominee_note for the organisers. A short "I've passed that on" is enough.

Nominating: the event must be open for nominations. Collect the nominee's name, category, phone
number and a photo (they can send it here; it arrives as "Image Url: ..."), then call nominate.

Complaints, refunds or problems you can't solve (not questions about an event): file_complaint with the details and any payment
ref from check_my_votes, and tell them the team will follow up. Don't promise refunds.

Who you're talking to: each turn comes with live context about the person texting: their WhatsApp
name, their verified number, how long and how much they've voted, their latest payments, anything
they've been nominated for, events they organise, complaints they've made, questions they've asked
the organisers, the events they've been chatting about, and notes you saved in earlier chats. Treat
them as someone you know. Use their first name now and then (it's their WhatsApp name, so drop it if
it isn't a name; if a note says what they like to be called, use that). Don't ask for anything the
context already answers, and use it to work out what they want: a nominee asking about their code, a
voter whose payment is still pending, someone following up on a complaint. Don't recite their history
back unprompted, and never share one person's details with anyone else. Organisers get no special
powers here; for changes to their event, point them to their dashboard on prestovotes.com.

Starting the conversation: if you haven't replied to them yet in this chat, open with their first
name, then, in one short line, the single most useful thing the context shows, if there is one: a
payment still waiting for them to approve, their nomination approved (with its voting code), an
organiser's answer to their question, or a complaint the team is following up. Then answer what they
asked. If they're new to PrestoVotes, welcome them in one line instead and say what you can help with
(voting, checking votes, nominating, flyers, questions about an event). Greet them like this once per
chat, not on every reply.

Remembering: when they tell you something worth knowing next time, save it with remember_voter_note:
what they like to be called, the language they prefer, who they're backing at an event, or an ongoing
situation ("travelling until 3 Dec"). Keep each note short and self-contained. Don't save what
PrestoVotes already tracks (payments, votes, nominations), or codes, PINs or sensitive personal
details. If a note is wrong or out of date, or they ask you to forget it, call forget_voter_note with
its [id] from the context. Don't announce that you're saving a note.
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
            client = (scope.get("client") or ("?",))[0]
            log.warning(f"rejected {scope.get('method')} {scope.get('path')} from {client}: {exc}")
            body = json.dumps({"error": "unauthorized", "detail": str(exc)}).encode()
            await send({"type": "http.response.start", "status": 401,
                        "headers": [(b"content-type", b"application/json")]})
            await send({"type": "http.response.body", "body": body})
            return
        await asgi_app(scope, receive, send)
    return gated


def _caller() -> dict[str, Any]:
    """Who Q says is texting (phone, WhatsApp name, whether the phone is verified), from the signed context."""
    claims = verify_context(get_http_headers(include={CONTEXT_HEADER}).get(CONTEXT_HEADER))
    return claims.get("user") or {}


def _voter_phone() -> str | None:
    """The sender's verified phone, from the context Q signed for this request."""
    user = _caller()
    return user.get("phone") if user.get("phone_verified") else None


async def _api(method: str, path: str, *, params: dict | None = None, body: dict | None = None) -> dict[str, Any]:
    headers = {"x-api-key": VOTER_KEY, "Accept": "application/json"}
    try:
        user = _caller()
    except ContextError as exc:
        log.warning(f"API {method} {path} not sent: the Q context didn't verify ({exc})")
        return {"success": False, "error": "This conversation couldn't be verified."}
    phone = user.get("phone") if user.get("phone_verified") else None
    if phone:
        headers["X-Voter-Phone"] = phone
        # Their WhatsApp name, so organisers see who a chat is with; percent-encoded, since
        # headers can't carry emoji or other non-latin characters.
        name = " ".join(str(user.get("name") or "").split())[:120]
        if name:
            headers["X-Voter-Name"] = quote(name)
    clean = {k: v for k, v in (params or {}).items() if v not in (None, "")}
    started = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS) as client:
            response = await client.request(method, f"{VOTER_API_URL}{path}", params=clean, json=body, headers=headers)
    except httpx.HTTPError as exc:
        log_api_call(log, method, path, started, None, f"{exc.__class__.__name__}: {exc}")
        return {"success": False, "error": "PrestoVotes isn't reachable right now. Please try again shortly."}
    try:
        payload = response.json()
    except ValueError:
        payload = {}
    error = payload.get("error") if response.status_code >= 400 else None
    if response.status_code >= 400 and not error:
        error = response.text[:300]
    log_api_call(log, method, path, started, response.status_code, error)
    if response.status_code >= 400:
        return {"success": False, "error": payload.get("error") or f"Something went wrong (HTTP {response.status_code})."}
    result = {"success": True, "data": payload.get("data")}
    if "meta" in payload:
        result["meta"] = {k: payload["meta"][k] for k in ("total", "page", "pages") if k in payload["meta"]}
    return result


mcp = FastMCP("PrestoVotes", instructions=INSTRUCTIONS)


def _who() -> str:
    """The texter, for the logs: masked number, or why there isn't one."""
    try:
        user = _caller()
    except ContextError:
        return "unverified context"
    if not user.get("phone"):
        return "no phone"
    return mask_phone(user["phone"]) + ("" if user.get("phone_verified") else " (unverified)")


mcp.add_middleware(ToolLogging(log, who=_who))


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
    An open event's categories, cost per vote and closing date, plus `about` (the organisers' own
    description of the election: dates, venue, how winners are chosen, tickets...) and `faq`
    (questions and answers they wrote). Use it for any question about the event. `whatsappGroup` is
    the organisers' WhatsApp group link for nominees (null if they haven't set one): give it to a
    nominee who asks how to join the group or reach the other nominees.

    Args:
        slug: the event's slug from find_events
    """
    return await _api("GET", f"/events/{slug}")


@mcp.tool(annotations=READ)
async def find_nominees(slug: str, q: str | None = None, category: str | None = None, page: int = 1) -> dict[str, Any]:
    """
    Approved nominees in an event, with their id (needed for start_vote and send_flyer), category,
    votingCode (the USSD code to dial) and votingLink. Vote counts are included unless the organisers
    have hidden results (then votes is null; don't guess).

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
                   description: str | None = None, institution: str | None = None,
                   whatsapp_number: str | None = None, whatsapp_consent: bool = False) -> dict[str, Any]:
    """
    Nominate someone in an event that's open for nominations. The nominee gets an SMS and the
    organisers review it before it appears. If you also pass their WhatsApp number with consent,
    we congratulate them on WhatsApp and say we'll let them know when voting starts.

    Before passing whatsapp_number, ask the voter whether the nominee agrees to the PrestoVotes
    WhatsApp AI messaging them, and only set whatsapp_consent=true when they say yes. If the voter
    is nominating themselves from this chat, their WhatsApp number is the one they're messaging from.

    Args:
        slug: the event's slug
        category: a category title from get_event
        name: the nominee's full name
        nominee_phone: the nominee's phone number
        photo_url: the photo the voter sent (the URL after "Image Url:")
        description: optional short reason or bio
        institution: optional school, company or group
        whatsapp_number: optional WhatsApp number to message the nominee on
        whatsapp_consent: true only once the voter confirms the nominee agrees to WhatsApp messages
    """
    return await _api("POST", "/nominations", body={
        "slug": slug, "category": category, "name": name, "nominee_phone": nominee_phone, "photo_url": photo_url,
        "description": description, "institution": institution,
        "whatsapp_number": whatsapp_number, "whatsapp_consent": whatsapp_consent is True})


@mcp.tool(annotations=WRITE)
async def send_flyer(nominee_id: int, kind: str | None = None) -> dict[str, Any]:
    """
    Draw a nominee's flyer and send it into this chat as a picture, for them to save and share. Works
    for any approved nominee, and for the texter's own nomination before it's approved. If it comes
    back with sent=false, give them the flyerUrl link instead. For their own nomination it also returns
    changePhotoUrl, but send nominees to the my-nominations page for any change (see instructions).

    Args:
        nominee_id: the nominee's id (from find_nominees, or from the live context for their own)
        kind: "voting" (with their voting code) or "nomination"; leave it out to get the right one
    """
    return await _api("POST", "/flyers/send", body={"nominee_id": nominee_id, "kind": kind})


@mcp.tool(annotations=WRITE)
async def save_nominee_note(nominee_id: int, note: str) -> dict[str, Any]:
    """
    Save a short note about a nominee for the event's organisers, who see it on the nominee's page:
    something useful a nominee or voter told you, such as achievements, the name they go by, what they
    want voters to know, or a mistake in their details. 3-500 characters, in plain words. Never save
    anything sensitive.

    Args:
        nominee_id: the nominee's id
        note: the note
    """
    return await _api("POST", f"/nominees/{nominee_id}/notes", body={"note": note})


@mcp.tool(annotations=WRITE)
async def remember_voter_note(note: str) -> dict[str, Any]:
    """
    Save one short note about the person texting, to remember in future chats: what they like to be
    called ("prefers to be called Kofi"), the language they prefer, who they're backing ("supporting
    Sarkodie at the music awards"), or an ongoing situation ("travelling until 3 Dec"). Notes come
    back in the context with their [id]. Don't save what PrestoVotes already tracks (payments, votes,
    nominations), codes or PINs, or sensitive personal details. Only the assistant and Presto staff
    see these; organisers don't.

    Args:
        note: the note, 3-300 characters, written so it makes sense on its own later
    """
    return await _api("POST", "/notes", body={"note": note})


@mcp.tool(annotations=WRITE)
async def forget_voter_note(note_id: int) -> dict[str, Any]:
    """
    Delete a note about the person texting that is wrong or no longer true, or that they ask you to
    forget. Notes in the context show their id in brackets, e.g. [12].

    Args:
        note_id: the note's id from the context
    """
    return await _api("DELETE", f"/notes/{note_id}")


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


@mcp.tool(annotations=WRITE)
async def ask_organisers(slug: str, question: str) -> dict[str, Any]:
    """
    Pass a question about an event to its organisers when get_event's about and faq don't answer it
    (e.g. the venue, dress code, ticket prices, prize details). They're alerted, and their answer is
    sent to this person on WhatsApp. For questions about the event, and for asking the organisers to
    check a nomination someone says they got but can't be found under their number (see the
    instructions); problems with payments or votes go to file_complaint.

    Args:
        slug: the event's slug, from find_events or get_event
        question: their question as one clear sentence, e.g. "Is there a dress code for the awards night?"
    """
    try:
        name = (_caller().get("name") or "").strip() or None
    except ContextError:
        name = None
    return await _api("POST", "/questions", body={"event": slug, "question": question, "name": name})


@mcp.resource("q://context", name="voter_context", mime_type="text/plain",
              description="Who is texting: their WhatsApp name, voting history, latest payments, nominations, "
                          "events they run, recent complaints and questions to organisers, events they've been "
                          "chatting about and notes remembered from earlier chats, given to the assistant before each turn.")
async def voter_context() -> str:
    try:
        user = _caller()
    except ContextError:
        return "This conversation couldn't be verified, so nothing is known about who is texting."
    who = _who_line(user)
    if not (user.get("phone") and user.get("phone_verified")):
        return who + "\nTheir number isn't verified (not on WhatsApp, or it's hidden), so voting and their history aren't available."
    result = await _api("GET", "/me")
    if not result.get("success"):
        # PrestoVotes without /me yet (older deploy): the payments and nominations calls still work.
        return "\n\n".join(part for part in [who, await _payments_context(), await _nominations_context()] if part).strip()
    me = result.get("data") or {}
    parts = [who, _history_text(me.get("voting")), _payments_text(me.get("recentPayments") or []),
             _nominations_text(me.get("nominations") or []), _runs_text(me.get("runs") or []),
             _complaints_text(me.get("complaints") or []), _questions_text(me.get("questions") or []),
             _recent_events_text(me.get("recentEvents") or []), _notes_text(me.get("notes") or [])]
    return "\n\n".join(p for p in parts if p).strip()


def _who_line(user: dict) -> str:
    name = (user.get("name") or "").strip()
    phone = user.get("phone")
    line = "Texting now: " + (f"{name} (their WhatsApp name)" if name else "someone with no WhatsApp name set")
    if phone:
        line += f", {phone}" + (" (verified by WhatsApp)" if user.get("phone_verified") else "")
    return line + "."


def _month(iso: str | None) -> str:
    try:
        return datetime.fromisoformat(iso).strftime("%b %Y") if iso else "?"
    except ValueError:
        return "?"


def _history_text(voting: dict | None) -> str:
    if not voting:
        return "New to PrestoVotes: no paid votes from this number yet."
    top_events = ", ".join(f"{e['event']} ({e['votes']:,} votes)" for e in voting.get("topEvents") or [])
    top_nominees = ", ".join(f"{n['nominee']} at {n['event']} ({n['votes']:,})" for n in voting.get("topNominees") or [])
    lines = [f"Voting with us since {_month(voting.get('firstVoteAt'))}, last voted {_month(voting.get('lastVoteAt'))}: "
             f"{voting.get('paidPayments', 0):,} paid payments, {voting.get('votes', 0):,} votes, GHS {voting.get('spent', 0):,.2f} in all."]
    if top_events:
        lines.append(f"Votes most in: {top_events}.")
    if top_nominees:
        lines.append(f"Top nominees they've backed: {top_nominees}.")
    return "\n".join(lines)


def _payments_text(rows: list) -> str:
    if not rows:
        return ""
    lines = [f"- {str(r.get('createdAt') or '')[:16].replace('T', ' ')} UTC: {r.get('votes')} vote{'' if r.get('votes') == 1 else 's'} for {r.get('nominee')} "
             f"({r.get('event')}), GHS {float(r.get('amount') or 0):.2f}, {r.get('status')}"
             + (f", ref {r['ref']}" if r.get("ref") else "") for r in rows]
    pending = sum(1 for r in rows if r.get("status") == "pending")
    head = ("Their only payment" if len(rows) == 1 else f"Their last {len(rows)} payments") \
        + (f" ({pending} waiting for approval)" if pending else "") + ":"
    return "\n".join([head, *lines])


def _nominations_text(rows: list) -> str:
    # A nominee replying to our congratulations message lands here, so say what they were nominated for.
    if not rows:
        return ""
    lines = [f"- {r['name']} (nominee id {r['id']}) for {r['category']} at {r['event']} (slug {r['slug']}): "
             + (f"approved, voting code {r['votingCode']}" + (", voting open" if r["votingOpen"] else ", voting not open yet")
                if r["approved"] else "waiting for the organisers to approve") for r in rows]
    return "\n".join(["This number has been nominated (we may have messaged them about it on WhatsApp):", *lines])


def _runs_text(rows: list) -> str:
    if not rows:
        return ""
    lines = [f"- {r['event']} (slug {r['slug']}), {r['role']}"
             + (", voting open" if r.get("votingOpen") else "") + (", nominations open" if r.get("nominationsOpen") else "")
             for r in rows]
    return "\n".join(["This number is on the account that runs these events:", *lines])


def _complaints_text(rows: list) -> str:
    if not rows:
        return ""
    lines = []
    for r in rows:
        line = f"- #{r['id']} on {str(r.get('createdAt') or '')[:10]}: {r.get('summary')}"
        if r.get("status") == "resolved":
            line += f" Resolved by the team on {str(r.get('resolvedAt') or '')[:10]}."
            if r.get("whatWeToldThem"):
                line += f" We sent them: {r['whatWeToldThem']}"
        else:
            line += " Still open: the team is following it up."
        lines.append(line)
    return "\n".join(["Complaints they've made recently:", *lines])


def _questions_text(rows: list) -> str:
    if not rows:
        return ""
    lines = []
    for r in rows:
        line = f"- {str(r.get('askedAt') or '')[:10]}, about {r.get('event')} (slug {r.get('slug')}): \"{r.get('question')}\""
        if r.get("status") == "answered":
            line += f" Organisers answered (and we sent it to them on WhatsApp): {r.get('answer')}"
        elif r.get("status") == "pending":
            line += " Waiting for the organisers to answer."
        else:
            line += " The organisers didn't answer this one."
        lines.append(line)
    return "\n".join(["Questions they've asked the organisers:", *lines])


ACTIONS = {"viewed_event": "asked about it", "viewed_nominees": "looked at nominees", "vote": "voted",
           "nomination": "nominated someone", "flyer": "got a flyer", "note": "left a nominee note",
           "complaint": "complained", "question": "asked the organisers"}


def _ago(iso: str | None) -> str:
    try:
        days = (datetime.utcnow() - datetime.fromisoformat(iso)).days if iso else None
    except ValueError:
        days = None
    if days is None:
        return ""
    return "today" if days < 1 else "yesterday" if days < 2 else f"{days} days ago"


def _recent_events_text(rows: list) -> str:
    if not rows:
        return ""
    parts = []
    for r in rows:
        bits = [b for b in (ACTIONS.get(r.get("lastAction") or "", ""), _ago(r.get("lastSeen"))) if b]
        parts.append(f"{r.get('event')} (slug {r.get('slug')})" + (f": {', '.join(bits)}" if bits else ""))
    return "Events they've been chatting with us about, latest first: " + "; ".join(parts) + "."


def _notes_text(rows: list) -> str:
    if not rows:
        return ""
    return "Notes remembered from earlier chats: " + "; ".join(f"[{r['id']}] {r['note']}" for r in rows)


async def _payments_context() -> str:
    result = await _api("GET", "/votes/mine", params={"limit": 5})
    if not result.get("success"):
        return "Couldn't load this voter's payments."
    rows = result.get("data") or []
    return _payments_text(rows) or "This voter hasn't voted on PrestoVotes from this number before."


async def _nominations_context() -> str:
    result = await _api("GET", "/nominations/mine")
    return _nominations_text((result.get("data") or []) if result.get("success") else [])


# RequestLogging is outermost, so requests require_q_context turns away are logged too.
app = RequestLogging(require_q_context(mcp.http_app(path="/mcp")), log.getChild("http"))

if __name__ == "__main__":
    # log_config=None keeps uvicorn's logs in our console format (mcp_logs.setup); RequestLogging
    # replaces its access log.
    uvicorn.run(app, host=os.getenv("HOST", "0.0.0.0"), port=int(os.getenv("PORT", "8012")),
                log_config=None, access_log=False)
