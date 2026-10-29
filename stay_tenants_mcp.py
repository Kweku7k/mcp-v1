"""FastMCP server for PrestoStay tenants.

Talks to the tenant endpoints in tenant_mcp_api.py over HTTP. One process =
one authenticated tenant session; every tool call here acts as that tenant.

Auth model: there is no login tool here. A tenant proves phone ownership via
POST /api/v1/tenant/otp/request + POST /api/v1/tenant/otp/verify (gated by
TENANT_API_INTERNAL_KEY, called by the WhatsApp bot backend - never by this
MCP server or the LLM). The resulting bearer token is handed to this process
via STAY_TENANT_TOKEN before it starts.

Run (one process per authenticated tenant session):
    STAY_API_BASE_URL=https://stay.prestoghana.com \
    STAY_TENANT_TOKEN=eyJ... \
    python fastmcp_tenant_server.py
"""

import os
from typing import Any, Dict, Optional

import requests

try:
    from mcp.server.fastmcp import FastMCP
except ImportError:  # Backward compatibility with older package naming
    from fastmcp import FastMCP


SERVER_NAME = "Presto Stay Tenants MCP"
DEFAULT_BASE_URL = "https://stay.prestoghana.com"
REQUEST_TIMEOUT_SECONDS = 20

TENANT_TOKEN = os.environ.get("STAY_TENANT_TOKEN")
if not TENANT_TOKEN:
    raise RuntimeError(
        "STAY_TENANT_TOKEN is required. It must be minted by calling "
        "POST /api/v1/tenant/otp/verify (after /otp/request) with a "
        "verified WhatsApp phone number, organisation, and OTP code, plus "
        "the TENANT_API_INTERNAL_KEY header - never something this MCP "
        "server or the LLM should do itself."
    )

BASE_URL = (os.environ.get("STAY_API_BASE_URL") or DEFAULT_BASE_URL).strip().rstrip("/")

mcp = FastMCP(SERVER_NAME)


def _request(method: str, endpoint: str, *, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    url = f"{BASE_URL}{endpoint}"
    headers = {"Authorization": f"Bearer {TENANT_TOKEN}"}

    try:
        if method.upper() == "GET":
            response = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT_SECONDS)
        else:
            headers["Content-Type"] = "application/json"
            response = requests.post(
                url, headers=headers, json=payload or {}, timeout=REQUEST_TIMEOUT_SECONDS
            )
    except requests.RequestException as request_error:
        return {
            "ok": False,
            "statusCode": 503,
            "endpoint": endpoint,
            "response": {
                "error": "Failed to reach PrestoStay API",
                "details": str(request_error),
            },
        }

    try:
        response_payload = response.json()
    except Exception:
        response_payload = {"raw": response.text}

    return {
        "ok": response.ok,
        "statusCode": response.status_code,
        "endpoint": endpoint,
        "response": response_payload,
    }

@mcp.tool()
def get_my_profile() -> Dict[str, Any]:
    """Get the tenant's own room assignment and financial summary."""
    return _request("GET", "/api/v1/tenant/me")


@mcp.tool()
def get_my_roommates() -> Dict[str, Any]:
    """Get roommate names and phone numbers for the tenant's current room."""
    return _request("GET", "/api/v1/tenant/me/roommates")


@mcp.tool()
def get_payment_guidance() -> Dict[str, Any]:
    """Get plain-language guidance on how the tenant should pay."""
    return _request("GET", "/api/v1/tenant/payment-guidance")


@mcp.tool()
def get_payment_link() -> Dict[str, Any]:
    """Get the tenant's current payment link and payment type (booking fee, regular, or renewal)."""
    return _request("POST", "/api/v1/tenant/payment-link")


@mcp.tool()
def file_complaint(message: str) -> Dict[str, Any]:
    """Submit a complaint or maintenance issue on behalf of the tenant.

    Args:
        message: description of the issue (10-2000 characters)
    """
    return _request("POST", "/api/v1/tenant/complaint", payload={"message": message})


if __name__ == "__main__":
    mcp.run(transport="http", port=8010)
