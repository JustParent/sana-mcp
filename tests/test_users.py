"""Resolving people to Sana user ids."""

from __future__ import annotations

import pytest

from sana_mcp import users, views


def test_classify_identifier_distinguishes_email_from_id():
    assert users.classify_identifier("priya@acme.com") == "email"
    assert users.classify_identifier("5_dl6jHhI78o") == "id"


def test_classify_identifier_rejects_empty_values():
    with pytest.raises(ValueError, match="non-empty"):
        users.classify_identifier("   ")


def test_resolve_user_id_passes_ids_through_without_a_call():
    def fetch(*args, **kwargs):
        raise AssertionError("ids should not need a lookup")

    assert users.resolve_user_id("u123", fetch=fetch) == "u123"


def test_resolve_user_id_looks_up_emails():
    def fetch(method, path, params=None):
        assert params["email"] == "priya@acme.com"
        return {"data": [{"id": "u1", "email": "priya@acme.com"}]}

    assert users.resolve_user_id("priya@acme.com", fetch=fetch) == "u1"


def test_resolve_user_id_reports_unknown_emails():
    def fetch(method, path, params=None):
        return {"data": []}

    with pytest.raises(ValueError, match="No Sana user found"):
        users.resolve_user_id("nobody@acme.com", fetch=fetch)


def test_resolve_user_ids_preserves_order():
    def fetch(method, path, params=None):
        return {"data": [{"id": "resolved-" + params["email"][0]}]}

    assert users.resolve_user_ids(["a@x.com", "u2", "b@x.com"], fetch=fetch) == [
        "resolved-a",
        "u2",
        "resolved-b",
    ]


def test_resolve_user_ids_rejects_empty_and_oversized_batches():
    with pytest.raises(ValueError, match="at least one"):
        users.resolve_user_ids([])

    too_many = [f"u{i}" for i in range(views.MAX_BULK_USERS + 1)]
    with pytest.raises(ValueError, match="At most"):
        users.resolve_user_ids(too_many)


def test_search_users_by_name_matches_case_insensitively():
    def fetch(method, path, params=None):
        return {
            "data": [
                {"id": "u1", "firstName": "Priya", "lastName": "Patel"},
                {"id": "u2", "firstName": "Sam", "lastName": "Jones"},
            ]
        }

    matches, truncated = users.search_users_by_name("priya", 10, fetch=fetch)
    assert [m["id"] for m in matches] == ["u1"]
    assert truncated is False


def test_search_users_by_name_stops_at_the_limit():
    def fetch(method, path, params=None):
        return {"data": [{"id": f"u{i}", "firstName": "Sam"} for i in range(10)]}

    matches, _ = users.search_users_by_name("sam", 3, fetch=fetch)
    assert len(matches) == 3
