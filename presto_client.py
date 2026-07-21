from typing import Any
import os
from pathlib import Path

import httpx

from config import PRESTOQ_API_TOKEN, PRESTOQ_API_URL


class PrestoQAPIError(Exception):
    """Raised when the PrestoQ API returns an unsuccessful response."""


class PrestoQClient:
    def __init__(self) -> None:
        self.base_url = PRESTOQ_API_URL
        self.headers = {
            "Accept": "application/json",
        }
        if PRESTOQ_API_TOKEN:
            self.headers["Authorization"] = f"Bearer {PRESTOQ_API_TOKEN}"

    def _set_token(self, token: str) -> None:
        self.headers["Authorization"] = f"Bearer {token}"
        # Keep both env var spellings available for compatibility.
        os.environ["PRESTOQ_API_TOKEN"] = token
        os.environ["PRESTO_API_TOKEN"] = token

    def _persist_token(self, token: str) -> None:
        env_path = Path(__file__).resolve().parent / ".env"

        if env_path.exists():
            raw_lines = env_path.read_text(encoding="utf-8").splitlines()
        else:
            raw_lines = []

        keys = {"PRESTOQ_API_TOKEN": token, "PRESTO_API_TOKEN": token}
        found = {key: False for key in keys}
        output_lines: list[str] = []

        for line in raw_lines:
            if "=" not in line:
                output_lines.append(line)
                continue

            key, _, _value = line.partition("=")
            key = key.strip()

            if key in keys:
                output_lines.append(f"{key}={keys[key]}")
                found[key] = True
            else:
                output_lines.append(line)

        for key, value in keys.items():
            if not found[key]:
                output_lines.append(f"{key}={value}")

        env_path.write_text("\n".join(output_lines).rstrip("\n") + "\n", encoding="utf-8")

    async def login(self, email: str, password: str) -> dict[str, Any]:
        url = f"{self.base_url}/signin"

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(
                    url,
                    json={"email": email, "password": password},
                    headers={"Accept": "application/json"},
                )

            response.raise_for_status()

        except httpx.TimeoutException as exc:
            raise PrestoQAPIError(
                "The PrestoQ signin request timed out."
            ) from exc

        except httpx.HTTPStatusError as exc:
            status_code = exc.response.status_code

            try:
                error_body = exc.response.json()
            except ValueError:
                error_body = exc.response.text

            raise PrestoQAPIError(
                f"PrestoQ signin returned HTTP {status_code}: {error_body}"
            ) from exc

        except httpx.RequestError as exc:
            raise PrestoQAPIError(
                f"Could not connect to the PrestoQ API: {exc}"
            ) from exc

        try:
            payload = response.json()
        except ValueError as exc:
            raise PrestoQAPIError(
                "The PrestoQ signin API returned invalid JSON."
            ) from exc

        token = ((payload.get("data") or {}).get("token") or "").strip()
        if not token:
            raise PrestoQAPIError("Signin response did not include data.token")

        self._set_token(token)
        self._persist_token(token)

        return {
            "success": True,
            "message": "Login successful.",
            "token": token,
        }

    async def get(
        self,
        path: str,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        url = f"{self.base_url}/{path.lstrip('/')}"

        try:
            async with httpx.AsyncClient(
                headers=self.headers,
                timeout=30.0,
            ) as client:
                response = await client.get(url, params=params)

            response.raise_for_status()

        except httpx.TimeoutException as exc:
            raise PrestoQAPIError(
                "The PrestoQ API request timed out."
            ) from exc

        except httpx.HTTPStatusError as exc:
            status_code = exc.response.status_code

            try:
                error_body = exc.response.json()
            except ValueError:
                error_body = exc.response.text

            raise PrestoQAPIError(
                f"PrestoQ API returned HTTP {status_code}: {error_body}"
            ) from exc

        except httpx.RequestError as exc:
            raise PrestoQAPIError(
                f"Could not connect to the PrestoQ API: {exc}"
            ) from exc

        try:
            return response.json()
        except ValueError as exc:
            raise PrestoQAPIError(
                "The PrestoQ API returned invalid JSON."
            ) from exc