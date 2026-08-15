"""Credential resolution and OAuth2 client-credentials token caching.

Sana issues short-lived (1 hour) bearer tokens from ``POST /api/token`` using the
client-credentials grant. This server is typically run as a long-lived subprocess
(a warm MCP sandbox can survive for hours), so the token is cached in memory and
refreshed proactively shortly before it expires.

All configuration comes from environment variables — there is no config file and
no interactive login, so the server works headlessly.
"""

from __future__ import annotations

import os
import sys
import threading
import time
from typing import Callable

DEFAULT_SCOPE = "read,write"
DEFAULT_CATALOG_TTL = 300.0

# Refresh this many seconds before the token actually expires, so a long-running
# request never races the expiry.
_TOKEN_REFRESH_SKEW = 120.0

# Fallback when Sana omits expiresIn from the token response.
_DEFAULT_EXPIRES_IN = 3600.0

_SETUP_HINT = (
    'In Sana, create an API client (Settings -> API), then set SANA_CLIENT_ID, '
    'SANA_CLIENT_SECRET and SANA_DOMAIN (your subdomain, e.g. "acme", or the full '
    "https://acme.sana.ai URL) in the server environment."
)


class AuthError(RuntimeError):
    """Raised when usable Sana credentials cannot be obtained."""


def _first_env(*names: str) -> str | None:
    """Return the first non-empty value among the given env var names."""
    for name in names:
        value = os.environ.get(name)
        if value and value.strip():
            return value.strip()
    return None


def resolve_base_url(value: str | None = None) -> str:
    """Normalize a Sana domain into an origin URL with no trailing path.

    Accepts a bare subdomain (``acme``), a host (``acme.sana.ai``) or a full URL
    (``https://acme.sana.ai`` or ``https://acme.sana.ai/api``). All request paths
    are built as ``{base}/api/...``, so any trailing ``/api`` is stripped here to
    keep both spellings equivalent.

    Args:
        value: The raw domain. Defaults to the ``SANA_DOMAIN`` env var (aliases
            ``SANA_BASE_URL`` / ``SANA_URL``).

    Returns:
        An origin such as ``https://acme.sana.ai``.

    Raises:
        AuthError: If no domain is configured.
        ValueError: If the domain is malformed or not https.
    """
    raw = value if value is not None else _first_env("SANA_DOMAIN", "SANA_BASE_URL", "SANA_URL")
    if not raw or not raw.strip():
        raise AuthError(f"SANA_DOMAIN is not set. {_SETUP_HINT}")

    raw = raw.strip().rstrip("/")
    if raw.startswith("http://"):
        raise ValueError(
            f"Sana must be reached over https (got {raw!r}). Use https:// or just the subdomain."
        )

    if raw.startswith("https://"):
        url = raw
    elif "." in raw:
        url = f"https://{raw}"
    else:
        # Bare subdomain, e.g. "acme".
        if "/" in raw or " " in raw:
            raise ValueError(f"Invalid Sana domain {raw!r}. Use a subdomain like 'acme'.")
        url = f"https://{raw}.sana.ai"

    url = url.rstrip("/")
    if url.endswith("/api"):
        url = url[: -len("/api")]
    url = url.rstrip("/")

    if url in ("https:/", "https://") or not url.startswith("https://") or len(url) <= len("https://"):
        raise ValueError(f"Invalid Sana domain {raw!r}. Use a subdomain like 'acme'.")
    return url


def client_id() -> str:
    """Return the configured OAuth2 client id.

    Raises:
        AuthError: If it is not set.
    """
    value = _first_env("SANA_CLIENT_ID", "CLIENT_ID")
    if not value:
        raise AuthError(f"SANA_CLIENT_ID is not set. {_SETUP_HINT}")
    return value


def client_secret() -> str:
    """Return the configured OAuth2 client secret.

    Raises:
        AuthError: If it is not set.
    """
    value = _first_env("SANA_CLIENT_SECRET", "CLIENT_SECRET")
    if not value:
        raise AuthError(f"SANA_CLIENT_SECRET is not set. {_SETUP_HINT}")
    return value


def scope() -> str:
    """Return the token scope (default ``read,write``; set ``read`` for read-only)."""
    return _first_env("SANA_SCOPE") or DEFAULT_SCOPE


def catalog_ttl() -> float:
    """Return the search-catalog cache TTL in seconds (default 300)."""
    raw = _first_env("SANA_CATALOG_TTL")
    if not raw:
        return DEFAULT_CATALOG_TTL
    try:
        return max(0.0, float(raw))
    except ValueError:
        return DEFAULT_CATALOG_TTL


class TokenCache:
    """In-memory bearer token with proactive refresh, safe across threads.

    FastMCP dispatches synchronous tool functions onto a worker-thread pool, so
    two tool calls can request a token concurrently. Double-checked locking keeps
    that to a single token fetch.
    """

    def __init__(self, now: Callable[[], float] = time.monotonic) -> None:
        self._now = now
        self._token: str | None = None
        self._expires_at = 0.0
        self._lock = threading.Lock()

    def invalidate(self) -> None:
        """Drop the cached token so the next call fetches a fresh one."""
        with self._lock:
            self._token = None
            self._expires_at = 0.0

    def get(self, fetch: Callable[[], tuple[str, float]]) -> str:
        """Return a valid bearer token, fetching one only when needed.

        Args:
            fetch: Callable returning ``(access_token, expires_in_seconds)``.

        Returns:
            The bearer token string.
        """
        token = self._token
        if token and self._now() < self._expires_at - _TOKEN_REFRESH_SKEW:
            return token

        with self._lock:
            # Another thread may have refreshed while we waited for the lock.
            token = self._token
            if token and self._now() < self._expires_at - _TOKEN_REFRESH_SKEW:
                return token

            access_token, expires_in = fetch()
            if not access_token:
                raise AuthError(
                    "Sana returned an empty access token. Check that the API client is "
                    "enabled for your domain."
                )
            try:
                lifetime = float(expires_in)
            except (TypeError, ValueError):
                lifetime = _DEFAULT_EXPIRES_IN
            if lifetime <= 0:
                lifetime = _DEFAULT_EXPIRES_IN

            self._token = access_token
            self._expires_at = self._now() + lifetime
            return access_token


# Process-wide token cache; the server is a single long-lived process.
token_cache = TokenCache()


def ensure_credentials() -> bool:
    """Validate configuration at startup without touching the network.

    Never raises and never writes to stdout: the MCP stdio channel must carry
    JSON-RPC only, and the server should always start so that individual tool
    calls can surface a clear :class:`AuthError`. The first token fetch happens
    lazily on the first tool call, where the error is actionable.

    Returns:
        True when all required settings are present and well-formed.
    """
    try:
        base_url = resolve_base_url()
        client_id()
        client_secret()
    except (AuthError, ValueError) as exc:
        print(f"sana-mcp: {exc}", file=sys.stderr)
        return False

    print(f"sana-mcp: configured for {base_url} (scope={scope()})", file=sys.stderr)
    return True
