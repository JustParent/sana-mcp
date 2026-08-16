"""Resolve people to Sana user ids.

Every tool that takes a person accepts either an email address or a user id, so
an assistant can act on "assign this to priya@acme.com" without a lookup step.
Email resolution costs one extra API call; ids pass through free.
"""

from __future__ import annotations

from typing import Any, Callable

from . import views
from .client import execute, get_paginated, unwrap


def classify_identifier(value: str) -> str:
    """Return ``"email"`` or ``"id"`` for a user identifier.

    Raises:
        ValueError: If the value is empty.
    """
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"User identifier must be a non-empty string (got {value!r}).")
    return "email" if "@" in value else "id"


def lookup_by_email(email: str, *, fetch: Callable[..., Any] | None = None) -> dict[str, Any] | None:
    """Return the user record for an email address, or None if there is no match."""
    fetch = fetch or execute
    payload = fetch("GET", "/api/v0/users", params={"email": email, "limit": 2})
    data = unwrap(payload)
    if isinstance(data, list):
        return data[0] if data else None
    return data or None


def resolve_user_id(user: str, *, fetch: Callable[..., Any] | None = None) -> str:
    """Resolve an email or id to a user id.

    Args:
        user: Email address or user id.
        fetch: Injected request function, for tests.

    Returns:
        The Sana user id.

    Raises:
        ValueError: If the identifier is empty or no user matches the email.
    """
    if classify_identifier(user) == "id":
        return user.strip()

    email = user.strip()
    record = lookup_by_email(email, fetch=fetch)
    if not record or not record.get("id"):
        raise ValueError(
            f"No Sana user found with email {email!r}. Use sana_find_user to search by name."
        )
    return str(record["id"])


def resolve_user_ids(users: list[str], *, fetch: Callable[..., Any] | None = None) -> list[str]:
    """Resolve a bounded batch of emails/ids to user ids.

    Args:
        users: Up to ``MAX_BULK_USERS`` emails or ids.
        fetch: Injected request function.

    Returns:
        User ids in the order given.

    Raises:
        ValueError: If the list is empty, too long, or an email does not resolve.
    """
    if not users:
        raise ValueError("`users` must contain at least one email address or user id.")
    if len(users) > views.MAX_BULK_USERS:
        raise ValueError(
            f"At most {views.MAX_BULK_USERS} users per call (got {len(users)}). "
            "Split the list across several calls."
        )
    return [resolve_user_id(user, fetch=fetch) for user in users]


def search_users_by_name(
    query: str, limit: int, *, fetch: Callable[..., Any] | None = None
) -> tuple[list[dict[str, Any]], bool]:
    """Scan users for a case-insensitive name or email substring match.

    Sana has no name-search parameter, so this pages through the directory up to
    ``MAX_USER_SEARCH_PAGES`` pages and matches locally.

    Returns:
        ``(matches, truncated)`` — ``truncated`` means the scan hit its page bound
        before exhausting the directory, so matches may be incomplete.
    """
    records, truncated = get_paginated(
        "/api/v0/users", max_pages=views.MAX_USER_SEARCH_PAGES, fetch=fetch or execute
    )
    needle = query.strip().lower()
    matches = []
    for record in records:
        if not isinstance(record, dict):
            continue
        haystack = " ".join(
            str(record.get(field) or "")
            for field in ("firstName", "lastName", "name", "email")
        ).lower()
        if needle in haystack:
            matches.append(record)
        if len(matches) >= limit:
            break
    return matches, truncated
