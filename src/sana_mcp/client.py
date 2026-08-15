"""Authenticated HTTP access to the Sana API.

Every request in this server funnels through :func:`execute`, which attaches the
bearer token, retries transient failures with bounded backoff, and refreshes the
token once on a 401. The retry budget is deliberately small (~5s worst case)
because MCP hosts cap individual tool calls at tens of seconds.
"""

from __future__ import annotations

import random
import time
from functools import lru_cache
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

import httpx

from .auth import AuthError, client_id, client_secret, resolve_base_url, scope, token_cache

# Backoff schedule: ~0.5, 1, 2s (with jitter), capped retries.
_MAX_RETRIES = 3
_BASE_DELAY = 0.5
_MAX_DELAY = 4.0
_RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})

_MAX_ERROR_CHARS = 500

ALLOWED_METHODS = ("GET", "POST", "PATCH", "PUT", "DELETE")


class ApiError(RuntimeError):
    """A non-retryable error response from the Sana API."""

    def __init__(self, status: int, message: str, path: str) -> None:
        super().__init__(f"Sana API {status} for {path}: {message}")
        self.status = status
        self.message = message
        self.path = path


@lru_cache(maxsize=1)
def get_client() -> httpx.Client:
    """Build (and cache) the HTTP client bound to the configured Sana domain.

    Cached for the lifetime of the process; ``httpx.Client`` is thread-safe.
    """
    return httpx.Client(
        base_url=resolve_base_url(),
        timeout=httpx.Timeout(20.0, connect=10.0),
        headers={"Accept": "application/json"},
    )


def fetch_token() -> tuple[str, float]:
    """Request a fresh access token with the client-credentials grant.

    Returns:
        ``(access_token, expires_in_seconds)``.

    Raises:
        AuthError: If Sana rejects the credentials or returns an unusable body.
    """
    client = get_client()
    try:
        response = client.post(
            "/api/token",
            data={
                "grant_type": "client_credentials",
                "client_id": client_id(),
                "client_secret": client_secret(),
                "scope": scope(),
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
    except httpx.HTTPError as exc:
        raise AuthError(f"Could not reach Sana at {client.base_url}: {exc}") from exc

    if response.status_code in (400, 401, 403):
        raise AuthError(
            f"Sana rejected the client credentials (HTTP {response.status_code} from "
            "/api/token). Check SANA_CLIENT_ID / SANA_CLIENT_SECRET, and that the API "
            "client is enabled for your domain."
        )
    if response.status_code >= 400:
        raise AuthError(
            f"Unexpected HTTP {response.status_code} from /api/token: "
            f"{_error_message(response)}"
        )

    payload = _json_or_error(response, "/api/token")
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        data = payload if isinstance(payload, dict) else {}
    access_token = data.get("accessToken") or data.get("access_token") or ""
    expires_in = data.get("expiresIn") or data.get("expires_in") or 3600
    return str(access_token), float(expires_in)


def _bearer() -> str:
    """Return a valid bearer token, fetching or refreshing as needed."""
    return token_cache.get(fetch_token)


def _error_message(response: httpx.Response) -> str:
    """Extract a human-readable message from an error response."""
    try:
        payload = response.json()
    except ValueError:
        return response.text[:_MAX_ERROR_CHARS].strip() or response.reason_phrase

    if isinstance(payload, dict):
        for key in ("message", "error", "detail", "title"):
            value = payload.get(key)
            if isinstance(value, str) and value:
                return value[:_MAX_ERROR_CHARS]
        errors = payload.get("errors")
        if errors:
            return str(errors)[:_MAX_ERROR_CHARS]
    return str(payload)[:_MAX_ERROR_CHARS]


def _json_or_error(response: httpx.Response, path: str) -> Any:
    """Parse a JSON body, tolerating the empty bodies some writes return."""
    if not response.content:
        return {}
    try:
        return response.json()
    except ValueError as exc:
        raise ApiError(response.status_code, f"non-JSON response ({exc})", path) from exc


def retry_delay(attempt: int, retry_after: str | None = None) -> float:
    """Return the backoff delay for a retry attempt, honoring ``Retry-After``.

    Args:
        attempt: Zero-based attempt number.
        retry_after: Raw ``Retry-After`` header value, if the server sent one.

    Returns:
        Seconds to sleep, never more than ``_MAX_DELAY``.
    """
    if retry_after:
        try:
            return min(max(float(retry_after), 0.0), _MAX_DELAY)
        except ValueError:
            pass
    delay = min(_BASE_DELAY * (2**attempt), _MAX_DELAY)
    # Full jitter avoids synchronized retries (thundering herd).
    return delay * (0.5 + random.random() / 2)


def is_retryable(status: int) -> bool:
    """Return True when a status code is worth retrying."""
    return status in _RETRYABLE_STATUS


def execute(
    method: str,
    path: str,
    *,
    params: dict[str, Any] | None = None,
    json_body: Any = None,
) -> Any:
    """Make an authenticated Sana API request with bounded retries.

    Args:
        method: HTTP method.
        path: Absolute API path, e.g. ``/api/v0/users``.
        params: Query parameters; ``None`` values are dropped.
        json_body: Optional JSON request body.

    Returns:
        The parsed JSON body (``{}`` for empty responses).

    Raises:
        AuthError: If credentials are missing or rejected.
        ApiError: For non-retryable errors or once retries are exhausted.
    """
    client = get_client()
    clean_params = {k: v for k, v in (params or {}).items() if v is not None}
    refreshed = False

    for attempt in range(_MAX_RETRIES + 1):
        headers = {"Authorization": f"Bearer {_bearer()}"}
        try:
            response = client.request(
                method.upper(),
                path,
                params=clean_params or None,
                json=json_body,
                headers=headers,
            )
        except httpx.HTTPError as exc:
            if attempt == _MAX_RETRIES:
                raise ApiError(0, f"network error: {exc}", path) from exc
            time.sleep(retry_delay(attempt))
            continue

        if response.status_code == 401 and not refreshed:
            # The cached token may have been revoked or expired early.
            token_cache.invalidate()
            refreshed = True
            continue

        if response.status_code == 401:
            raise AuthError(
                "Sana rejected the access token twice (HTTP 401). Check that the API "
                "client is still enabled and that SANA_SCOPE covers this operation."
            )

        if is_retryable(response.status_code) and attempt < _MAX_RETRIES:
            time.sleep(retry_delay(attempt, response.headers.get("Retry-After")))
            continue

        if response.status_code >= 400:
            raise ApiError(response.status_code, _error_message(response), path)

        return _json_or_error(response, path)

    raise ApiError(0, "retries exhausted", path)


def unwrap(payload: Any) -> Any:
    """Return the ``data`` member of a Sana envelope, or the payload itself."""
    if isinstance(payload, dict) and "data" in payload:
        return payload["data"]
    return payload


def next_cursor(payload: Any) -> str | None:
    """Extract the pagination cursor for the next page, if there is one.

    Sana returns ``links.next`` as a full URL carrying a ``next`` query parameter.
    A bare top-level ``next`` string is also accepted.
    """
    if not isinstance(payload, dict):
        return None

    links = payload.get("links")
    if isinstance(links, dict):
        nxt = links.get("next")
        if isinstance(nxt, str) and nxt:
            query = parse_qs(urlparse(nxt).query)
            values = query.get("next")
            if values and values[0]:
                return values[0]
            return None

    nxt = payload.get("next")
    if isinstance(nxt, str) and nxt:
        return nxt
    return None


def get_paginated(
    path: str,
    params: dict[str, Any] | None = None,
    *,
    max_pages: int,
    page_limit: int = 1000,
    fetch: Callable[..., Any] = execute,
) -> tuple[list[Any], bool]:
    """Follow Sana's cursor pagination up to a bounded number of pages.

    Args:
        path: API path to page through.
        params: Extra query parameters.
        max_pages: Hard bound on pages fetched, so output stays finite.
        page_limit: Page size requested (Sana allows up to 1000).
        fetch: Injected for testing; defaults to :func:`execute`.

    Returns:
        ``(items, truncated)`` where ``truncated`` is True if more pages remain.
    """
    items: list[Any] = []
    cursor: str | None = None

    for _ in range(max(1, max_pages)):
        page_params = dict(params or {})
        page_params["limit"] = page_limit
        if cursor:
            page_params["next"] = cursor

        payload = fetch("GET", path, params=page_params)
        data = unwrap(payload)
        if isinstance(data, list):
            items.extend(data)
        elif data:
            items.append(data)

        cursor = next_cursor(payload)
        if not cursor:
            return items, False

    return items, True


def close_client() -> None:
    """Close and drop the cached HTTP client (used between tests)."""
    if get_client.cache_info().currsize:
        get_client().close()
    get_client.cache_clear()
