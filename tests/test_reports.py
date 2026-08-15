"""Report job guardrails and bounded polling."""

from __future__ import annotations

import pytest

from sana_mcp import reports


def test_clamp_wait_bounds_the_budget():
    assert reports.clamp_wait(0) == 0
    assert reports.clamp_wait(5) == 5
    assert reports.clamp_wait(999) == reports.MAX_WAIT_SECONDS
    assert reports.clamp_wait(-3) == 0
    assert reports.clamp_wait("nonsense") == 10


def test_validate_insights_sql_accepts_select_and_with():
    query = 'SELECT "user" FROM "analytics"."user_course_instance_progress"'
    assert reports.validate_insights_sql(query) == query
    assert reports.validate_insights_sql("WITH x AS (SELECT 1) SELECT * FROM x")


def test_validate_insights_sql_ignores_leading_comments():
    assert reports.validate_insights_sql("-- completions\nSELECT 1")


def test_validate_insights_sql_rejects_writes():
    with pytest.raises(ValueError, match="read-only"):
        reports.validate_insights_sql('DELETE FROM "analytics"."x"')


def test_validate_insights_sql_rejects_multiple_statements():
    with pytest.raises(ValueError, match="one SQL statement"):
        reports.validate_insights_sql("SELECT 1; DROP TABLE users")


def test_validate_insights_sql_allows_a_trailing_semicolon():
    assert reports.validate_insights_sql("SELECT 1;")


def test_validate_insights_sql_rejects_empty_input():
    with pytest.raises(ValueError, match="non-empty"):
        reports.validate_insights_sql("   ")


def test_is_terminal_and_has_download():
    assert reports.is_terminal("successful")
    assert reports.is_terminal("FAILED")
    assert not reports.is_terminal("pending")
    assert not reports.is_terminal(None)

    assert reports.has_download({"link": {"url": "https://x"}})
    assert not reports.has_download({"link": None})
    assert not reports.has_download({})


def test_poll_job_returns_immediately_with_no_budget():
    calls = []

    def fetch(method, path, params=None):
        calls.append(path)
        return {"data": {"jobId": "j1", "status": "pending"}}

    job = reports.poll_job("/api/v1/reports/jobs/j1", wait_seconds=0, fetch=fetch)
    assert job["status"] == "pending"
    assert len(calls) == 1


def test_poll_job_stops_once_the_job_finishes():
    responses = [
        {"data": {"jobId": "j1", "status": "pending"}},
        {"data": {"jobId": "j1", "status": "successful", "link": {"url": "https://d"}}},
    ]
    calls = []

    def fetch(method, path, params=None):
        calls.append(path)
        return responses[min(len(calls) - 1, len(responses) - 1)]

    job = reports.poll_job(
        "/api/v1/reports/jobs/j1", wait_seconds=10, fetch=fetch, sleep=lambda s: None
    )
    assert job["status"] == "successful"
    assert len(calls) == 2


def test_find_learner_progress_report_id():
    def fetch(method, path, params=None):
        return {"data": [{"id": "learner-progress", "title": "Learner Progress"}]}

    assert reports.find_learner_progress_report_id(fetch=fetch) == "learner-progress"


def test_find_learner_progress_report_id_errors_when_none_exist():
    def fetch(method, path, params=None):
        return {"data": []}

    with pytest.raises(ValueError, match="no reports"):
        reports.find_learner_progress_report_id(fetch=fetch)
