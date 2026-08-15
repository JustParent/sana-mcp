"""Shared fixtures.

Every test runs offline: no network, no credentials. The fixture below clears
Sana environment variables and resets process-wide caches so tests cannot leak
state into each other.
"""

from __future__ import annotations

import pytest

from sana_mcp import auth, client, search

_ENV_VARS = (
    "SANA_DOMAIN",
    "SANA_BASE_URL",
    "SANA_URL",
    "SANA_CLIENT_ID",
    "CLIENT_ID",
    "SANA_CLIENT_SECRET",
    "CLIENT_SECRET",
    "SANA_SCOPE",
    "SANA_CATALOG_TTL",
)


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch):
    """Clear Sana env vars and cached state around every test."""
    for name in _ENV_VARS:
        monkeypatch.delenv(name, raising=False)

    client.get_client.cache_clear()
    auth.token_cache.invalidate()
    search.clear_cache()
    yield
    client.get_client.cache_clear()
    auth.token_cache.invalidate()
    search.clear_cache()
