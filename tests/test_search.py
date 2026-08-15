"""Catalog ranking, filtering, and caching — the grounding path."""

from __future__ import annotations

import pytest

from sana_mcp import search


def _items():
    return [
        {
            "id": "c1",
            "title": "Leadership Essentials",
            "description": "Core skills for new managers.",
            "tags": ["leadership"],
            "contentType": "course",
        },
        {
            "id": "c2",
            "title": "Giving Feedback",
            "description": "How leadership conversations work.",
            "tags": ["communication"],
            "contentType": "course",
        },
        {
            "id": "c3",
            "title": "Advanced Excel",
            "description": "Pivot tables and formulas.",
            "tags": ["leadership", "data"],
            "contentType": "course",
        },
    ]


def test_parse_content_types_defaults_to_everything():
    assert search.parse_content_types(None) == list(search.CONTENT_TYPES)
    assert search.parse_content_types("  ") == list(search.CONTENT_TYPES)


def test_parse_content_types_normalizes_and_orders():
    assert search.parse_content_types("path, course") == ["course", "path"]
    assert search.parse_content_types("PROGRAM") == ["program"]


def test_parse_content_types_rejects_unknown_values():
    with pytest.raises(ValueError, match="content_types"):
        search.parse_content_types("course,video")


def test_tokenize_splits_on_punctuation():
    assert search.tokenize("Leadership, essentials!") == ["leadership", "essentials"]
    assert search.tokenize(None) == []


def test_title_match_outranks_tag_then_description():
    ranked = search.rank_items(_items(), "leadership", 10)
    assert [item["id"] for item in ranked] == ["c1", "c3", "c2"]
    assert ranked[0]["score"] > ranked[1]["score"] > ranked[2]["score"]


def test_non_matching_items_are_excluded():
    ranked = search.rank_items(_items(), "kubernetes", 10)
    assert ranked == []


def test_empty_query_browses_alphabetically():
    ranked = search.rank_items(_items(), None, 10)
    assert [item["title"] for item in ranked] == [
        "Advanced Excel",
        "Giving Feedback",
        "Leadership Essentials",
    ]
    assert "score" not in ranked[0]


def test_ranking_respects_the_limit():
    assert len(search.rank_items(_items(), "leadership", 1)) == 1


def test_equal_scores_break_ties_by_title():
    items = [
        {"id": "b", "title": "Zebra Skills", "tags": ["x"], "description": ""},
        {"id": "a", "title": "Alpha Skills", "tags": ["x"], "description": ""},
    ]
    ranked = search.rank_items(items, "skills", 10)
    assert [item["id"] for item in ranked] == ["a", "b"]


def test_phrase_in_title_beats_scattered_tokens():
    items = [
        {"id": "scattered", "title": "Feedback for Giving Managers", "description": ""},
        {"id": "phrase", "title": "Giving Feedback", "description": ""},
    ]
    ranked = search.rank_items(items, "giving feedback", 10)
    assert ranked[0]["id"] == "phrase"


def test_filter_by_tags_is_case_insensitive():
    filtered = search.filter_by_tags(_items(), ["LEADERSHIP"])
    assert {item["id"] for item in filtered} == {"c1", "c3"}


def test_filter_by_tags_passes_everything_when_unset():
    assert search.filter_by_tags(_items(), None) == _items()
    assert search.filter_by_tags(_items(), []) == _items()


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def _fetch_one_course(method, path, params=None):
    return {"data": [{"id": "c1", "title": "Cached Course"}]}


def test_catalog_is_cached_within_the_ttl():
    calls = []

    def fetch(method, path, params=None):
        calls.append(path)
        return _fetch_one_course(method, path, params)

    clock = FakeClock()
    items, truncated = search.get_catalog(["course"], fetch=fetch, now=clock)
    assert items[0]["title"] == "Cached Course"
    assert items[0]["contentType"] == "course"
    assert truncated is False

    clock.now = 10.0  # Inside the default 300s TTL.
    search.get_catalog(["course"], fetch=fetch, now=clock)
    assert len(calls) == 1


def test_catalog_refetches_after_the_ttl_expires():
    calls = []

    def fetch(method, path, params=None):
        calls.append(path)
        return _fetch_one_course(method, path, params)

    clock = FakeClock()
    search.get_catalog(["course"], fetch=fetch, now=clock)
    clock.now = 10_000.0
    search.get_catalog(["course"], fetch=fetch, now=clock)
    assert len(calls) == 2


def test_refresh_bypasses_the_cache():
    calls = []

    def fetch(method, path, params=None):
        calls.append(path)
        return _fetch_one_course(method, path, params)

    clock = FakeClock()
    search.get_catalog(["course"], fetch=fetch, now=clock)
    search.get_catalog(["course"], refresh=True, fetch=fetch, now=clock)
    assert len(calls) == 2


def test_find_courses_maps_ids_to_views():
    def fetch(method, path, params=None):
        return {"data": [{"id": "c1", "title": "One"}, {"id": "c2", "title": "Two"}]}

    found = search.find_courses(["c2", "missing"], fetch=fetch)
    assert found["c2"]["title"] == "Two"
    assert "missing" not in found
