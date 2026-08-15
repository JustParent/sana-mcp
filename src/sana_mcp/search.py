"""Local search over the Sana content catalog.

Sana's public API has no search endpoint, so grounding an answer in real content
means fetching the catalog (courses, paths, programs) and ranking it here. The
catalog is cached in-process for a short TTL because it is the hot path for
question answering and changes slowly.

Ranking is intentionally simple and transparent: a title match outranks a tag
match, which outranks a description match. Everything in this module below the
cache is pure, so it is unit-testable without network access.
"""

from __future__ import annotations

import re
import threading
import time
from typing import Any, Callable

from . import views
from .auth import catalog_ttl
from .client import get_paginated

CONTENT_TYPES = ("course", "path", "program")

_ENDPOINTS = {
    "course": ("/api/v0/courses", views.course_view),
    "path": ("/api/v0/paths", views.path_view),
    "program": ("/api/v0/programs", views.program_view),
}

# Scoring weights, highest signal first.
_PHRASE_IN_TITLE = 8
_TOKEN_IN_TITLE = 4
_TOKEN_IN_TAG = 2
_TOKEN_IN_DESCRIPTION = 1

_TOKEN_RE = re.compile(r"[a-z0-9]+")

_cache: dict[str, dict[str, Any]] = {}
_cache_lock = threading.Lock()


def parse_content_types(raw: str | None) -> list[str]:
    """Parse a comma-separated content-type filter.

    Args:
        raw: e.g. ``"course,path"``. Empty or None means all types.

    Returns:
        A list of valid content types, in canonical order.

    Raises:
        ValueError: If any entry is not a known content type.
    """
    if not raw or not raw.strip():
        return list(CONTENT_TYPES)

    requested = [part.strip().lower() for part in raw.split(",") if part.strip()]
    unknown = [part for part in requested if part not in CONTENT_TYPES]
    if unknown:
        allowed = ", ".join(CONTENT_TYPES)
        raise ValueError(f"content_types must be a subset of [{allowed}] (got {raw!r}).")
    return [t for t in CONTENT_TYPES if t in requested]


def tokenize(text: str | None) -> list[str]:
    """Split text into lowercase alphanumeric tokens."""
    if not text:
        return []
    return _TOKEN_RE.findall(text.lower())


def score_item(item: dict[str, Any], phrase: str, tokens: list[str]) -> int:
    """Score one catalog record against a query.

    Args:
        item: A trimmed content view.
        phrase: The whole lowercased query, for exact-phrase boosting.
        tokens: Query tokens.

    Returns:
        A non-negative relevance score; 0 means no match.
    """
    title = (item.get("title") or "").lower()
    description = (item.get("description") or "").lower()
    tags = [str(tag).lower() for tag in (item.get("tags") or [])]

    score = 0
    if phrase and phrase in title:
        score += _PHRASE_IN_TITLE

    title_tokens = set(tokenize(title))
    description_tokens = set(tokenize(description))
    tag_tokens = set()
    for tag in tags:
        tag_tokens.update(tokenize(tag))

    for token in set(tokens):
        if token in title_tokens:
            score += _TOKEN_IN_TITLE
        if token in tag_tokens:
            score += _TOKEN_IN_TAG
        if token in description_tokens:
            score += _TOKEN_IN_DESCRIPTION
    return score


def rank_items(items: list[dict[str, Any]], query: str | None, limit: int) -> list[dict[str, Any]]:
    """Rank catalog records by relevance to ``query``.

    With no query the catalog is listed alphabetically by title, which makes
    ``sana_search_content`` double as a browse tool.

    Args:
        items: Trimmed content views (each carrying a ``contentType`` key).
        query: Free-text query, or None to browse.
        limit: Maximum records to return.

    Returns:
        Up to ``limit`` records; matches carry a ``score`` field.
    """
    if not query or not query.strip():
        ordered = sorted(items, key=lambda i: (i.get("title") or "").lower())
        return ordered[:limit]

    phrase = query.strip().lower()
    tokens = tokenize(phrase)

    scored = []
    for item in items:
        score = score_item(item, phrase, tokens)
        if score > 0:
            scored.append((score, item))

    # Sort by score, then title, so equal scores come back in a stable order.
    scored.sort(key=lambda pair: (-pair[0], (pair[1].get("title") or "").lower()))
    return [dict(item, score=score) for score, item in scored[:limit]]


def filter_by_tags(items: list[dict[str, Any]], tags: list[str] | None) -> list[dict[str, Any]]:
    """Keep only records carrying at least one of ``tags`` (case-insensitive)."""
    if not tags:
        return items
    wanted = {tag.strip().lower() for tag in tags if tag and tag.strip()}
    if not wanted:
        return items
    kept = []
    for item in items:
        item_tags = {str(tag).lower() for tag in (item.get("tags") or [])}
        if item_tags & wanted:
            kept.append(item)
    return kept


def _fetch_catalog(content_type: str, fetch: Callable[..., Any]) -> dict[str, Any]:
    """Fetch and trim one content type's full catalog."""
    path, viewer = _ENDPOINTS[content_type]
    items, truncated = get_paginated(
        path, max_pages=views.MAX_CATALOG_PAGES, fetch=fetch
    )
    trimmed = []
    for item in items:
        view = viewer(item)
        if view:
            view["contentType"] = content_type
            trimmed.append(view)
    return {"items": trimmed, "fetched_at": time.monotonic(), "truncated": truncated}


def get_catalog(
    content_types: list[str],
    *,
    refresh: bool = False,
    fetch: Callable[..., Any] | None = None,
    now: Callable[[], float] = time.monotonic,
) -> tuple[list[dict[str, Any]], bool]:
    """Return the cached content catalog, fetching any stale or missing types.

    Args:
        content_types: Which types to include.
        refresh: Bypass the cache and refetch.
        fetch: Injected request function (defaults to the real client).
        now: Injected clock, for tests.

    Returns:
        ``(items, truncated)`` where ``truncated`` is True if any type hit the
        page bound and so may be missing content.
    """
    from .client import execute  # Imported lazily so tests can run without httpx setup.

    fetch = fetch or execute
    ttl = catalog_ttl()
    items: list[dict[str, Any]] = []
    truncated = False

    for content_type in content_types:
        with _cache_lock:
            entry = _cache.get(content_type)
            fresh = (
                entry is not None
                and not refresh
                and (now() - entry["fetched_at"]) < ttl
            )
        if not fresh:
            entry = _fetch_catalog(content_type, fetch)
            with _cache_lock:
                _cache[content_type] = entry

        items.extend(entry["items"])
        truncated = truncated or bool(entry["truncated"])

    return items, truncated


def find_courses(course_ids: list[str], *, fetch: Callable[..., Any] | None = None) -> dict[str, dict[str, Any]]:
    """Look up course summaries by id from the cached catalog.

    Args:
        course_ids: Course ids to resolve.
        fetch: Injected request function.

    Returns:
        Mapping of course id to its trimmed view (missing ids are absent).
    """
    items, _ = get_catalog(["course"], fetch=fetch)
    wanted = set(course_ids)
    return {item["id"]: item for item in items if item.get("id") in wanted}


def clear_cache() -> None:
    """Drop the cached catalog (used between tests and by ``refresh=True``)."""
    with _cache_lock:
        _cache.clear()
