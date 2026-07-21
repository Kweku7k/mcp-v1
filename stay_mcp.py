from typing import Any
import os

import httpx
from dotenv import load_dotenv
from fastmcp import FastMCP
import uvicorn


load_dotenv()


def _extract_bearer_token(scope: dict[str, Any]) -> str | None:
	"""Return the bearer token from the Authorization header if present."""
	for key, value in scope.get("headers", []):
		if key.lower() == b"authorization":
			authorization = value.decode("utf-8", errors="ignore").strip()
			if not authorization:
				return None
			parts = authorization.split(" ", 1)
			if len(parts) != 2:
				return None
			scheme, token = parts
			if scheme.lower() != "bearer":
				return None
			return token.strip() or None
	return None


def _extract_host(scope: dict[str, Any]) -> str | None:
	"""Return the request host from the Host header."""
	for key, value in scope.get("headers", []):
		if key.lower() == b"host":
			host = value.decode("utf-8", errors="ignore").strip().lower()
			if not host:
				return None
			return host
	return None

def with_allowed_hosts(asgi_app, allowed_hosts: set[str]):
    """Allow specific proxied hosts by remapping Host to localhost for MCP transport checks."""
    async def host_app(scope, receive, send):
        if scope.get("type") != "http":
            await asgi_app(scope, receive, send)
            return

        host = _extract_host(scope)
        if host and host in {h.lower() for h in allowed_hosts}:
            new_scope = dict(scope)
            new_headers = []

            for key, value in scope.get("headers", []):
                if key.lower() == b"host":
                    new_headers.append((b"host", b"localhost:8000"))
                else:
                    new_headers.append((key, value))

            new_scope["headers"] = new_headers
            await asgi_app(new_scope, receive, send)
            return

        await asgi_app(scope, receive, send)

    return host_app


def _json_response(status: int, body: bytes):
	"""Build a small ASGI JSON HTTP response."""
	return [
		{
			"type": "http.response.start",
			"status": status,
			"headers": [
				(b"content-type", b"application/json"),
				(b"www-authenticate", b"Bearer"),
			],
		},
		{
			"type": "http.response.body",
			"body": body,
		},
	]


def with_bearer_auth(asgi_app, expected_token: str):
	"""Wrap an ASGI app and enforce bearer auth for HTTP requests."""
	async def secured_app(scope, receive, send):
		if scope.get("type") != "http":
			await asgi_app(scope, receive, send)
			return

		provided_token = _extract_bearer_token(scope)

		if not provided_token:
			for message in _json_response(
				401,
				b'{"success":false,"error":"Missing bearer token."}',
			):
				await send(message)
			return

		if provided_token != expected_token:
			for message in _json_response(
				403,
				b'{"success":false,"error":"Invalid bearer token."}',
			):
				await send(message)
			return

		await asgi_app(scope, receive, send)

	return secured_app


class StayAPIError(Exception):
	"""Raised when the Stay MCP Data API request fails."""


class StayClient:
	def __init__(self) -> None:
		base = os.getenv("STAY_MCP_API_URL", "https://stay.prestoghana.com/api/v1/mcp")
		self.base_url = base.rstrip("/")
		self.api_key = os.getenv("STAY_API_KEY", "").strip()

	def set_api_key(self, api_key: str) -> None:
		self.api_key = api_key.strip()
		os.environ["STAY_API_KEY"] = self.api_key

	def _headers(self) -> dict[str, str]:
		headers = {"Accept": "application/json"}
		if self.api_key:
			headers["x-api-key"] = self.api_key
		return headers

	async def get_rooms(
		self,
		listing_slug: str | None = None,
		q: str | None = None,
		include_tenants: bool | None = None,
		page: int = 1,
		page_size: int = 50,
	) -> dict[str, Any]:
		if not self.api_key:
			raise StayAPIError(
				"STAY_API_KEY is missing. Set it in .env or call set_stay_api_key first."
			)

		if page < 1:
			raise StayAPIError("page must be at least 1")

		if page_size < 1 or page_size > 200:
			raise StayAPIError("page_size must be between 1 and 200")

		params: dict[str, Any] = {
			"page": page,
			"pageSize": page_size,
		}

		if listing_slug:
			params["listingSlug"] = listing_slug
		if q:
			params["q"] = q
		if include_tenants is not None:
			params["includeTenants"] = str(include_tenants).lower()

		url = f"{self.base_url}/rooms"

		try:
			async with httpx.AsyncClient(timeout=30.0) as client:
				response = await client.get(
					url,
					params=params,
					headers=self._headers(),
				)

			response.raise_for_status()

		except httpx.TimeoutException as exc:
			raise StayAPIError("The Stay API request timed out.") from exc

		except httpx.HTTPStatusError as exc:
			status_code = exc.response.status_code
			try:
				error_body = exc.response.json()
			except ValueError:
				error_body = exc.response.text

			if status_code == 401:
				raise StayAPIError("Missing API key (HTTP 401).") from exc
			if status_code == 403:
				raise StayAPIError("Invalid API key (HTTP 403).") from exc
			if status_code == 503:
				raise StayAPIError(
					"API key verification service unavailable (HTTP 503)."
				) from exc

			raise StayAPIError(
				f"Stay API returned HTTP {status_code}: {error_body}"
			) from exc

		except httpx.RequestError as exc:
			raise StayAPIError(f"Could not connect to Stay API: {exc}") from exc

		try:
			return response.json()
		except ValueError as exc:
			raise StayAPIError("The Stay API returned invalid JSON.") from exc

	async def get_tenants(
		self,
		sublisting_id: str | None = None,
		room_number: str | None = None,
		status: str | None = None,
		q: str | None = None,
		min_balance: float | None = None,
		max_balance: float | None = None,
		sort_by: str | None = None,
		sort_order: str = "asc",
		page: int = 1,
		page_size: int = 50,
	) -> dict[str, Any]:
		if not self.api_key:
			raise StayAPIError(
				"STAY_API_KEY is missing. Set it in .env or call set_stay_api_key first."
			)

		if page < 1:
			raise StayAPIError("page must be at least 1")

		if page_size < 1 or page_size > 200:
			raise StayAPIError("page_size must be between 1 and 200")

		sort_order_clean = (sort_order or "asc").lower()
		if sort_order_clean not in {"asc", "desc"}:
			raise StayAPIError("sort_order must be asc or desc")

		params: dict[str, Any] = {
			"page": page,
			"pageSize": page_size,
			"sortOrder": sort_order_clean,
		}

		if sublisting_id:
			params["sublistingId"] = sublisting_id
		if room_number:
			params["roomNumber"] = room_number
		if status:
			params["status"] = status
		if q:
			params["q"] = q
		if min_balance is not None:
			params["minBalance"] = min_balance
		if max_balance is not None:
			params["maxBalance"] = max_balance
		if sort_by:
			params["sortBy"] = sort_by

		url = f"{self.base_url}/tenants"

		try:
			async with httpx.AsyncClient(timeout=30.0) as client:
				response = await client.get(
					url,
					params=params,
					headers=self._headers(),
				)

			response.raise_for_status()

		except httpx.TimeoutException as exc:
			raise StayAPIError("The Stay API request timed out.") from exc

		except httpx.HTTPStatusError as exc:
			status_code = exc.response.status_code
			try:
				error_body = exc.response.json()
			except ValueError:
				error_body = exc.response.text

			if status_code == 401:
				raise StayAPIError("Missing API key (HTTP 401).") from exc
			if status_code == 403:
				raise StayAPIError("Invalid API key (HTTP 403).") from exc
			if status_code == 503:
				raise StayAPIError(
					"API key verification service unavailable (HTTP 503)."
				) from exc

			raise StayAPIError(
				f"Stay API returned HTTP {status_code}: {error_body}"
			) from exc

		except httpx.RequestError as exc:
			raise StayAPIError(f"Could not connect to Stay API: {exc}") from exc

		try:
			return response.json()
		except ValueError as exc:
			raise StayAPIError("The Stay API returned invalid JSON.") from exc

	async def assign_room(
		self,
		user_id: int,
		sublisting_id: int,
	) -> dict[str, Any]:
		if not self.api_key:
			raise StayAPIError(
				"STAY_API_KEY is missing. Set it in .env or call set_stay_api_key first."
			)

		if user_id <= 0:
			raise StayAPIError("user_id must be a positive integer")

		if sublisting_id <= 0:
			raise StayAPIError("sublisting_id must be a positive integer")

		url = f"{self.base_url}/assign"

		try:
			async with httpx.AsyncClient(timeout=30.0) as client:
				response = await client.post(
					url,
					json={"userId": user_id, "sublistingId": sublisting_id},
					headers=self._headers(),
				)

			response.raise_for_status()

		except httpx.TimeoutException as exc:
			raise StayAPIError("The Stay API request timed out.") from exc

		except httpx.HTTPStatusError as exc:
			status_code = exc.response.status_code
			try:
				error_body = exc.response.json()
			except ValueError:
				error_body = exc.response.text

			if status_code == 401:
				raise StayAPIError("Missing API key (HTTP 401).") from exc
			if status_code == 403:
				raise StayAPIError("Invalid API key (HTTP 403).") from exc
			if status_code == 503:
				raise StayAPIError(
					"API key verification service unavailable (HTTP 503)."
				) from exc

			raise StayAPIError(
				f"Stay API returned HTTP {status_code}: {error_body}"
			) from exc

		except httpx.RequestError as exc:
			raise StayAPIError(f"Could not connect to Stay API: {exc}") from exc

		try:
			return response.json()
		except ValueError as exc:
			raise StayAPIError("The Stay API returned invalid JSON.") from exc

	async def unassign_room(self, user_id: int) -> dict[str, Any]:
		if not self.api_key:
			raise StayAPIError(
				"STAY_API_KEY is missing. Set it in .env or call set_stay_api_key first."
			)

		if user_id <= 0:
			raise StayAPIError("user_id must be a positive integer")

		url = f"{self.base_url}/unassign"

		try:
			async with httpx.AsyncClient(timeout=30.0) as client:
				response = await client.post(
					url,
					json={"userId": user_id},
					headers=self._headers(),
				)

			response.raise_for_status()

		except httpx.TimeoutException as exc:
			raise StayAPIError("The Stay API request timed out.") from exc

		except httpx.HTTPStatusError as exc:
			status_code = exc.response.status_code
			try:
				error_body = exc.response.json()
			except ValueError:
				error_body = exc.response.text

			if status_code == 401:
				raise StayAPIError("Missing API key (HTTP 401).") from exc
			if status_code == 403:
				raise StayAPIError("Invalid API key (HTTP 403).") from exc
			if status_code == 503:
				raise StayAPIError(
					"API key verification service unavailable (HTTP 503)."
				) from exc

			raise StayAPIError(
				f"Stay API returned HTTP {status_code}: {error_body}"
			) from exc

		except httpx.RequestError as exc:
			raise StayAPIError(f"Could not connect to Stay API: {exc}") from exc

		try:
			return response.json()
		except ValueError as exc:
			raise StayAPIError("The Stay API returned invalid JSON.") from exc


mcp = FastMCP("Presto Stay MCP")
stay_client = StayClient()


@mcp.tool()
async def set_stay_api_key(api_key: str) -> dict[str, Any]:
	"""
	Set the Stay API key for this process.
	"""
	if not api_key.strip():
		return {
			"success": False,
			"error": "api_key cannot be empty",
		}

	stay_client.set_api_key(api_key)
	return {
		"success": True,
		"message": "Stay API key set for current process.",
	}


@mcp.tool()
async def get_rooms(
	listing_slug: str | None = None,
	q: str | None = None,
	include_tenants: bool | None = None,
	page: int = 1,
	page_size: int = 50,
) -> dict[str, Any]:
	"""
	Fetch room-level occupancy and bed status from Stay.
	"""
	try:
		api_response = await stay_client.get_rooms(
			listing_slug=listing_slug,
			q=q,
			include_tenants=include_tenants,
			page=page,
			page_size=page_size,
		)

		return {
			"success": True,
			"resource": api_response.get("resource", "rooms"),
			"meta": api_response.get("meta", {}),
			"count": len(api_response.get("data", []) or []),
			"data": api_response.get("data", []),
		}
	except StayAPIError as exc:
		return {
			"success": False,
			"error": str(exc),
			"data": [],
		}


@mcp.tool()
async def get_tenants(
	sublisting_id: str | None = None,
	room_number: str | None = None,
	status: str | None = None,
	q: str | None = None,
	min_balance: float | None = None,
	max_balance: float | None = None,
	sort_by: str | None = None,
	sort_order: str = "asc",
	page: int = 1,
	page_size: int = 50,
) -> dict[str, Any]:
	"""
	Fetch tenant records from Stay with filtering and sorting.
	"""
	try:
		api_response = await stay_client.get_tenants(
			sublisting_id=sublisting_id,
			room_number=room_number,
			status=status,
			q=q,
			min_balance=min_balance,
			max_balance=max_balance,
			sort_by=sort_by,
			sort_order=sort_order,
			page=page,
			page_size=page_size,
		)

		return {
			"success": True,
			"resource": api_response.get("resource", "tenants"),
			"meta": api_response.get("meta", {}),
			"count": len(api_response.get("data", []) or []),
			"data": api_response.get("data", []),
		}
	except StayAPIError as exc:
		return {
			"success": False,
			"error": str(exc),
			"data": [],
		}


@mcp.tool()
async def assign_room(user_id: int, sublisting_id: int) -> dict[str, Any]:
	"""
	Assign a tenant to a room in the listing resolved from the API key.
	"""
	try:
		api_response = await stay_client.assign_room(
			user_id=user_id,
			sublisting_id=sublisting_id,
		)

		return {
			"success": True,
			"status": api_response.get("status", "ok"),
			"action": api_response.get("action", "assigned"),
			"listingSlug": api_response.get("listingSlug"),
			"data": api_response.get("data", {}),
		}
	except StayAPIError as exc:
		return {
			"success": False,
			"error": str(exc),
			"data": {},
		}


@mcp.tool()
async def unassign_room(user_id: int) -> dict[str, Any]:
	"""
	Clear a tenant room assignment in the listing resolved from the API key.
	"""
	try:
		api_response = await stay_client.unassign_room(user_id=user_id)

		return {
			"success": True,
			"status": api_response.get("status", "ok"),
			"action": api_response.get("action", "unassigned"),
			"listingSlug": api_response.get("listingSlug"),
			"data": api_response.get("data", {}),
		}
	except StayAPIError as exc:
		return {
			"success": False,
			"error": str(exc),
			"data": {},
		}

if __name__ == "__main__":
    mcp.run(transport="http", port=8010)