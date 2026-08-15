"""Credential resolution and token caching.

No network is touched: the token cache takes an injected fetch callable and an
injected clock.
"""

from __future__ import annotations

import pytest

from sana_mcp.auth import (
    AuthError,
    TokenCache,
    catalog_ttl,
    client_id,
    client_secret,
    ensure_credentials,
    resolve_base_url,
    scope,
)


def test_resolve_base_url_accepts_bare_subdomain():
    assert resolve_base_url("acme") == "https://acme.sana.ai"


def test_resolve_base_url_accepts_host_and_full_url():
    assert resolve_base_url("acme.sana.ai") == "https://acme.sana.ai"
    assert resolve_base_url("https://acme.sana.ai") == "https://acme.sana.ai"


def test_resolve_base_url_strips_trailing_slash_and_api_suffix():
    assert resolve_base_url("https://acme.sana.ai/") == "https://acme.sana.ai"
    assert resolve_base_url("https://acme.sana.ai/api") == "https://acme.sana.ai"
    assert resolve_base_url("https://acme.sana.ai/api/") == "https://acme.sana.ai"


def test_resolve_base_url_rejects_plain_http():
    with pytest.raises(ValueError, match="https"):
        resolve_base_url("http://acme.sana.ai")


def test_resolve_base_url_requires_configuration():
    with pytest.raises(AuthError, match="SANA_DOMAIN"):
        resolve_base_url()


def test_env_alias_precedence(monkeypatch):
    monkeypatch.setenv("SANA_BASE_URL", "https://fallback.sana.ai")
    assert resolve_base_url() == "https://fallback.sana.ai"

    monkeypatch.setenv("SANA_DOMAIN", "primary")
    assert resolve_base_url() == "https://primary.sana.ai"


def test_client_credentials_read_aliases(monkeypatch):
    monkeypatch.setenv("CLIENT_ID", "alias-id")
    monkeypatch.setenv("CLIENT_SECRET", "alias-secret")
    assert client_id() == "alias-id"
    assert client_secret() == "alias-secret"

    monkeypatch.setenv("SANA_CLIENT_ID", "primary-id")
    assert client_id() == "primary-id"


def test_missing_credentials_name_the_fix():
    with pytest.raises(AuthError) as excinfo:
        client_id()
    message = str(excinfo.value)
    assert "SANA_CLIENT_ID" in message
    assert "SANA_DOMAIN" in message  # the setup hint names every variable


def test_scope_and_ttl_defaults(monkeypatch):
    assert scope() == "read,write"
    assert catalog_ttl() == 300.0

    monkeypatch.setenv("SANA_SCOPE", "read")
    monkeypatch.setenv("SANA_CATALOG_TTL", "60")
    assert scope() == "read"
    assert catalog_ttl() == 60.0

    monkeypatch.setenv("SANA_CATALOG_TTL", "not-a-number")
    assert catalog_ttl() == 300.0


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_token_cache_reuses_token_until_refresh_skew():
    clock = FakeClock()
    cache = TokenCache(now=clock)
    calls = []

    def fetch():
        calls.append(1)
        return f"token-{len(calls)}", 3600.0

    assert cache.get(fetch) == "token-1"
    clock.now += 3000  # Still well inside the hour.
    assert cache.get(fetch) == "token-1"
    assert len(calls) == 1


def test_token_cache_refreshes_before_expiry():
    clock = FakeClock()
    cache = TokenCache(now=clock)
    calls = []

    def fetch():
        calls.append(1)
        return f"token-{len(calls)}", 3600.0

    assert cache.get(fetch) == "token-1"
    # Inside the 120s refresh skew, so a new token is fetched early.
    clock.now += 3500
    assert cache.get(fetch) == "token-2"
    assert len(calls) == 2


def test_token_cache_invalidate_forces_refetch():
    cache = TokenCache(now=FakeClock())
    cache.get(lambda: ("first", 3600.0))
    cache.invalidate()
    assert cache.get(lambda: ("second", 3600.0)) == "second"


def test_token_cache_rejects_empty_token():
    cache = TokenCache(now=FakeClock())
    with pytest.raises(AuthError, match="empty access token"):
        cache.get(lambda: ("", 3600.0))


def test_token_cache_falls_back_on_bad_expiry():
    clock = FakeClock()
    cache = TokenCache(now=clock)
    assert cache.get(lambda: ("token", "nonsense")) == "token"
    clock.now += 100
    # The fallback lifetime (3600s) is in force, so the token is still valid.
    assert cache.get(lambda: ("other", 3600.0)) == "token"


def test_ensure_credentials_reports_failure_without_raising(capsys):
    assert ensure_credentials() is False
    assert "SANA_DOMAIN" in capsys.readouterr().err


def test_ensure_credentials_succeeds_and_logs_to_stderr(monkeypatch, capsys):
    monkeypatch.setenv("SANA_DOMAIN", "acme")
    monkeypatch.setenv("SANA_CLIENT_ID", "id")
    monkeypatch.setenv("SANA_CLIENT_SECRET", "secret")

    assert ensure_credentials() is True
    captured = capsys.readouterr()
    assert "https://acme.sana.ai" in captured.err
    # stdout carries JSON-RPC only.
    assert captured.out == ""
