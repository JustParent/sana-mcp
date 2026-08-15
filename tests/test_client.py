"""Pure helpers in the HTTP layer: envelopes, cursors, retries, pagination."""

from __future__ import annotations

from sana_mcp.client import get_paginated, is_retryable, next_cursor, retry_delay, unwrap


def test_unwrap_returns_data_member():
    assert unwrap({"data": [1, 2]}) == [1, 2]


def test_unwrap_tolerates_plain_payloads():
    assert unwrap({"id": "abc"}) == {"id": "abc"}
    assert unwrap([1]) == [1]


def test_next_cursor_reads_links_next_url():
    payload = {"links": {"next": "https://acme.sana.ai/api/v0/users?limit=100&next=CURSOR2"}}
    assert next_cursor(payload) == "CURSOR2"


def test_next_cursor_handles_top_level_and_missing():
    assert next_cursor({"next": "CURSOR3"}) == "CURSOR3"
    assert next_cursor({"data": []}) is None
    assert next_cursor({"links": {}}) is None
    assert next_cursor("not-a-dict") is None


def test_next_cursor_returns_none_when_link_has_no_cursor():
    assert next_cursor({"links": {"next": "https://acme.sana.ai/api/v0/users"}}) is None


def test_is_retryable_covers_rate_limit_and_server_errors():
    assert is_retryable(429)
    assert is_retryable(503)
    assert not is_retryable(404)
    assert not is_retryable(200)


def test_retry_delay_grows_and_stays_bounded():
    assert 0 < retry_delay(0) <= 0.5
    assert retry_delay(5) <= 4.0


def test_retry_delay_honors_retry_after_header():
    assert retry_delay(0, "2") == 2.0
    # Capped at the maximum delay.
    assert retry_delay(0, "600") == 4.0
    # Junk falls back to exponential backoff.
    assert 0 < retry_delay(0, "soon") <= 0.5


def _page(items, cursor=None):
    payload = {"data": items}
    if cursor:
        payload["links"] = {"next": f"https://acme.sana.ai/api/v0/x?next={cursor}"}
    return payload


def test_get_paginated_follows_cursors_to_the_end():
    pages = [_page([1, 2], "c1"), _page([3, 4], "c2"), _page([5])]
    seen = []

    def fetch(method, path, params=None):
        seen.append(params.get("next"))
        return pages[len(seen) - 1]

    items, truncated = get_paginated("/api/v0/x", max_pages=5, fetch=fetch)
    assert items == [1, 2, 3, 4, 5]
    assert truncated is False
    assert seen == [None, "c1", "c2"]


def test_get_paginated_stops_at_max_pages_and_reports_truncation():
    def fetch(method, path, params=None):
        return _page([1], "always-more")

    items, truncated = get_paginated("/api/v0/x", max_pages=2, fetch=fetch)
    assert items == [1, 1]
    assert truncated is True


def test_get_paginated_requests_the_page_limit():
    captured = {}

    def fetch(method, path, params=None):
        captured.update(params)
        return _page([1])

    get_paginated("/api/v0/x", {"email": "a@b.c"}, max_pages=1, page_limit=250, fetch=fetch)
    assert captured["limit"] == 250
    assert captured["email"] == "a@b.c"
