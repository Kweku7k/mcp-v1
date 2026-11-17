"""Logging for the MCP servers: what each one is asked to do, what it asks the product APIs, and
every error.

Call setup("<server>") once at startup, before the server runs, and add ToolLogging to the FastMCP
app. Everything (tool calls, resource reads, API calls, HTTP requests and replies, errors with
tracebacks) goes to the console (stdout); nothing is written to files here. Where it ends up
(journald, a log file, a log service) is the server's job.

Settings: LOG_LEVEL (default INFO; DEBUG also logs tool results).

Phone numbers are masked (233****0000) and tokens never logged; long text arguments are cut short.
"""

import json
import logging
import os
import re
import sys
import time
from typing import Any, Callable

from fastmcp.server.middleware import Middleware, MiddlewareContext

FORMAT = "%(asctime)s %(levelname)-7s [%(name)s] %(message)s"
# Whole key names (token, access_token, apiKey...), so MCP's progressToken still shows.
SENSITIVE = re.compile(r"(^|_)(token|secret|password|api_?key|authorization|code_verifier)$", re.I)
PHONE_KEYS = re.compile(r"phone|number|msisdn", re.I)
MAX_VALUE_CHARS = 200

_configured = False


def setup(server: str) -> logging.Logger:
    """Sends the root and uvicorn loggers to the console (stdout), in one format."""
    global _configured
    log = logging.getLogger(server)
    if _configured:
        return log
    _configured = True

    level = getattr(logging, os.getenv("LOG_LEVEL", "INFO").strip().upper(), logging.INFO)
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(logging.Formatter(FORMAT))

    root = logging.getLogger()
    root.setLevel(level)
    for handler in root.handlers[:]:
        root.removeHandler(handler)
    root.addHandler(console)

    # Uvicorn: let its loggers flow up to root (run it with log_config=None so it doesn't replace them).
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logging.getLogger(name).handlers.clear()
        logging.getLogger(name).propagate = True

    # httpx logs every request at INFO, the MCP SDK every message ("Processing request of type ...");
    # the servers log their API and tool calls themselves.
    for name in ("httpx", "httpcore", "mcp.server.lowlevel.server"):
        logging.getLogger(name).setLevel(logging.WARNING)

    sys.excepthook = lambda *exc: log.critical("unhandled exception", exc_info=exc)
    log.info(f"logging to the console (level {logging.getLevelName(level)})")
    return log


def mask_phone(value: Any) -> str:
    digits = re.sub(r"\D", "", str(value or ""))
    if len(digits) < 7:
        return "***" if digits else ""
    return f"{digits[:3]}{'*' * (len(digits) - 7)}{digits[-4:]}"


def clean(value: Any, key: str = "") -> Any:
    """A copy of tool arguments that is safe to log."""
    if key and SENSITIVE.search(key):
        return "***"
    if key and PHONE_KEYS.search(key) and isinstance(value, (str, int)):
        return mask_phone(value)
    if isinstance(value, dict):
        return {k: clean(v, str(k)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [clean(v) for v in value[:20]]
    if isinstance(value, str) and len(value) > MAX_VALUE_CHARS:
        return value[:MAX_VALUE_CHARS] + f"... ({len(value)} chars)"
    return value


def short(value: Any, limit: int = 500) -> str:
    try:
        text = json.dumps(value, default=str, ensure_ascii=False)
    except (TypeError, ValueError):
        text = str(value)
    return text if len(text) <= limit else text[:limit] + f"... ({len(text)} chars)"


def _failure(result: Any) -> str | None:
    """The error message when a tool returned {"success": false, "error": ...} instead of raising."""
    data = getattr(result, "structured_content", None)
    if isinstance(data, dict) and isinstance(data.get("result"), dict):
        data = data["result"]
    if isinstance(data, dict) and data.get("success") is False:
        return str(data.get("error") or "failed")
    return None


class ToolLogging(Middleware):
    """Logs every tool call and resource read: who, what, how long, and how it went.

    who() returns a short description of the caller (e.g. a masked phone or the token's scope);
    it's called inside the request, so it can read headers or the access token."""

    def __init__(self, log: logging.Logger, who: Callable[[], str] | None = None) -> None:
        self.log = log
        self.who = who

    def _caller(self) -> str:
        if self.who is None:
            return ""
        try:
            return f" by {self.who()}"
        except Exception as exc:  # never let logging break a request
            return f" by ? ({exc.__class__.__name__})"

    async def on_call_tool(self, context: MiddlewareContext, call_next):
        name = context.message.name
        args = short(clean(context.message.arguments or {}))
        caller = self._caller()
        started = time.perf_counter()
        try:
            result = await call_next(context)
        except Exception:
            ms = (time.perf_counter() - started) * 1000
            self.log.exception(f"tool {name}{caller} raised after {ms:.0f}ms args={args}")
            raise
        ms = (time.perf_counter() - started) * 1000
        error = _failure(result)
        if error:
            self.log.warning(f"tool {name}{caller} failed in {ms:.0f}ms: {error} args={args}")
        else:
            self.log.info(f"tool {name}{caller} ok in {ms:.0f}ms args={args}")
            if self.log.isEnabledFor(logging.DEBUG):
                self.log.debug(f"tool {name} result: {short(getattr(result, 'structured_content', None) or result, 2000)}")
        return result

    async def on_read_resource(self, context: MiddlewareContext, call_next):
        uri = str(context.message.uri)
        caller = self._caller()
        started = time.perf_counter()
        try:
            result = await call_next(context)
        except Exception:
            self.log.exception(f"resource {uri}{caller} raised after {(time.perf_counter() - started) * 1000:.0f}ms")
            raise
        self.log.info(f"resource {uri}{caller} ok in {(time.perf_counter() - started) * 1000:.0f}ms")
        return result


BODY_CHARS = int(os.getenv("LOG_BODY_CHARS", "4000"))
MASK_PHONES = os.getenv("LOG_MASK_PHONES", "1").strip().lower() not in {"0", "false", "no", "off"}
LONG_NUMBER = re.compile(r"(?<![\d*])\+?\d{10,13}(?!\d)")
SESSION_HEADER = b"mcp-session-id"


def _mask_text(text: str) -> str:
    return LONG_NUMBER.sub(lambda m: mask_phone(m.group()), text) if MASK_PHONES else text


def _rpc_summary(message: Any) -> str:
    """'tools/call find_events' / 'initialize' / 'result #3' for one JSON-RPC message."""
    if not isinstance(message, dict):
        return ""
    method = message.get("method")
    if method:
        params = message.get("params") or {}
        target = params.get("name") or params.get("uri") or ""
        return f"{method} {target}".strip()
    if "error" in message:
        return f"error #{message['id']}" if message.get("id") is not None else "error"
    return f"result #{message.get('id')}" if "id" in message else ""


def _slim_result(message: Any) -> Any:
    """Drops what only bloats the log: the tool list's schemas, and a tool result's text copy of
    its structured content."""
    if not isinstance(message, dict) or not isinstance(message.get("result"), dict):
        return message
    result = message["result"]
    if isinstance(result.get("tools"), list):
        return {**message, "result": {"tools": [t.get("name") for t in result["tools"] if isinstance(t, dict)]}}
    if "structuredContent" in result:
        slim = {"structuredContent": result["structuredContent"]}
        if result.get("isError"):
            slim["isError"] = True
        return {**message, "result": slim}
    return message


def _parse_body(raw: bytes, content_type: str) -> list[Any]:
    """The JSON messages in a request/response body: plain JSON, or the data: lines of an SSE stream."""
    text = raw.decode("utf-8", "replace")
    if "text/event-stream" in content_type:
        messages = []
        for line in text.splitlines():
            if line.startswith("data:"):
                try:
                    messages.append(json.loads(line[5:].strip()))
                except ValueError:
                    messages.append(line[5:].strip())
        return messages
    if not text.strip():
        return []
    try:
        parsed = json.loads(text)
    except ValueError:
        return [text]
    return parsed if isinstance(parsed, list) else [parsed]


def _describe(messages: list[Any]) -> tuple[str, str]:
    """(summary, body) for logging."""
    summary = ", ".join(s for s in (_rpc_summary(m) for m in messages) if s)
    cleaned = [clean(_slim_result(m)) for m in messages]
    body = short(cleaned[0] if len(cleaned) == 1 else cleaned, BODY_CHARS) if cleaned else ""
    return summary, _mask_text(body)


class RequestLogging:
    """ASGI middleware that logs every HTTP request to the server and its reply:

        → #12 POST /mcp from 41.66.1.2 [Claude-User 1.0 / claude-ai] auth=bearer pva_AbCd1234...
              tools/call get_event {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {...}}
        ← #12 200 in 182ms result #3 {"jsonrpc": "2.0", "id": 3, "result": {"structuredContent": {...}}}

    "from" is the client IP (X-Forwarded-For when behind nginx), then the MCP client's name and
    version from its initialize message (remembered per MCP session) and the User-Agent. Tokens
    are never logged in full, phone-like numbers are masked (LOG_MASK_PHONES=0 to turn that off),
    and bodies are cut at LOG_BODY_CHARS (default 4000). Wrap the outermost app, so requests the
    auth layer turns away are logged too."""

    def __init__(self, app, log: logging.Logger) -> None:
        self.app = app
        self.log = log
        self.counter = 0
        self.clients: dict[str, str] = {}   # MCP session id -> "name version" from initialize

    @staticmethod
    def _headers(scope) -> dict[str, str]:
        return {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}

    @staticmethod
    def _auth(headers: dict[str, str]) -> str:
        parts = []
        scheme, _, token = headers.get("authorization", "").partition(" ")
        if token:
            parts.append(f"{scheme.lower()} {token[:12]}...")
        if headers.get("x-api-key"):
            parts.append(f"api-key {headers['x-api-key'][:12]}...")
        if headers.get("x-q-context"):
            parts.append("q-context")
        return " auth=" + (", ".join(parts) if parts else "none")

    def _client_info(self, messages: list[Any]) -> str | None:
        for m in messages:
            if isinstance(m, dict) and m.get("method") == "initialize":
                info = (m.get("params") or {}).get("clientInfo") or {}
                return f"{info.get('name') or '?'} {info.get('version') or ''}".strip()
        return None

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        self.counter += 1
        rid = f"#{self.counter}"
        headers = self._headers(scope)
        ip = headers.get("x-forwarded-for", "").split(",")[0].strip() or (scope.get("client") or ("?",))[0]
        session = headers.get("mcp-session-id")
        path = scope.get("path", "") + (f"?{scope['query_string'].decode('latin-1')}" if scope.get("query_string") else "")
        started = time.perf_counter()
        request_body, response_body = bytearray(), bytearray()
        state = {"request_logged": False, "response_logged": False, "status": None, "content_type": "",
                 "session": session, "client": None}
        limit = BODY_CHARS * 4   # bytes kept from each body; the rest is counted, not stored

        def log_request():
            if state["request_logged"]:
                return
            state["request_logged"] = True
            messages = _parse_body(bytes(request_body), headers.get("content-type", ""))
            state["client"] = self._client_info(messages)
            client = state["client"] or (self.clients.get(session) if session else None)
            who = " / ".join(x for x in (client, headers.get("user-agent", "")[:80]) if x)
            summary, body = _describe(messages)
            line = f"→ {rid} {scope.get('method')} {path} from {ip}" + (f" [{who}]" if who else "")
            line += self._auth(headers) + (f" session={session[:8]}" if session else "")
            if summary or body:
                line += f"\n      {summary} {body}".rstrip()
            self.log.info(line)

        def log_response(note=""):
            if state["response_logged"]:
                return
            state["response_logged"] = True
            ms = (time.perf_counter() - started) * 1000
            status = state["status"]
            summary, body = _describe(_parse_body(bytes(response_body), state["content_type"]))
            line = f"← {rid} {status or 'no response'} in {ms:.0f}ms{note}"
            if summary or body:
                line += f" {summary} {body}".rstrip()
            level = logging.ERROR if not status or status >= 500 else logging.WARNING if status >= 400 else logging.INFO
            self.log.log(level, line)

        async def logged_receive():
            message = await receive()
            if message["type"] == "http.request":
                if len(request_body) < limit:
                    request_body.extend(message.get("body", b"")[: limit - len(request_body)])
                if not message.get("more_body"):
                    log_request()
            return message

        async def logged_send(message):
            if message["type"] == "http.response.start":
                log_request()   # GETs and early rejections send before reading a body
                state["status"] = message["status"]
                for k, v in message.get("headers", []):
                    if k.lower() == b"content-type":
                        state["content_type"] = v.decode("latin-1")
                    elif k.lower() == SESSION_HEADER:
                        state["session"] = v.decode("latin-1")
                if state["client"] and state["session"]:
                    if len(self.clients) > 5000:
                        self.clients.clear()
                    self.clients[state["session"]] = state["client"]
            elif message["type"] == "http.response.body":
                if len(response_body) < limit:
                    response_body.extend(message.get("body", b"")[: limit - len(response_body)])
                if not message.get("more_body"):
                    await send(message)
                    log_response()
                    return
            await send(message)

        try:
            await self.app(scope, logged_receive, logged_send)
        except Exception:
            log_request()
            self.log.exception(f"← {rid} crashed after {(time.perf_counter() - started) * 1000:.0f}ms")
            raise
        log_request()
        log_response(" (stream closed)" if state["status"] else "")


def log_api_call(log: logging.Logger, method: str, url: str, started: float, status: int | None,
                 error: str | None = None) -> None:
    """One line per call to a product API. 4xx is a warning (often expected: closed event, bad
    input); 5xx, timeouts and connection failures are errors."""
    ms = (time.perf_counter() - started) * 1000
    if error:  # one line, even when the API sent back an HTML error page
        error = " ".join(re.sub(r"<[^>]+>", " ", str(error)).split())[:300]
    if status is None:
        log.error(f"API {method} {url} failed after {ms:.0f}ms: {error}")
    elif status >= 500:
        log.error(f"API {method} {url} -> {status} in {ms:.0f}ms: {error or ''}".rstrip(": "))
    elif status >= 400:
        log.warning(f"API {method} {url} -> {status} in {ms:.0f}ms: {error or ''}".rstrip(": "))
    else:
        log.info(f"API {method} {url} -> {status} in {ms:.0f}ms")
