"""Async report jobs: insights SQL and the built-in learner-progress report.

Both Sana reporting APIs are asynchronous: you create a job, then poll until a
download link appears. MCP hosts cap a tool call at tens of seconds, so these
helpers poll only for a short bounded window and hand the job id back when the
report is still running — the caller resumes with ``sana_get_report_job``.
"""

from __future__ import annotations

import re
import time
from typing import Any, Callable

from .client import execute, unwrap

# Upper bound on in-tool polling, well inside a typical 30s tool deadline.
MAX_WAIT_SECONDS = 20
POLL_INTERVAL = 2.0

TERMINAL_STATUSES = frozenset({"successful", "success", "completed", "failed", "error"})

_LEADING_NOISE_RE = re.compile(r"^\s*(--[^\n]*\n|/\*.*?\*/|\s)+", re.DOTALL)


def clamp_wait(seconds: Any) -> int:
    """Clamp a requested wait to the 0..``MAX_WAIT_SECONDS`` range."""
    try:
        value = int(seconds)
    except (TypeError, ValueError):
        return 10
    return max(0, min(value, MAX_WAIT_SECONDS))


def validate_insights_sql(sql: str) -> str:
    """Check that an insights query is a single read-only statement.

    The insights endpoint is a SQL surface over the analytics warehouse. This is
    a client-side guardrail against obvious mistakes (a stray ``DELETE``, two
    statements pasted together); Sana enforces its own permissions server-side.

    Args:
        sql: The query text.

    Returns:
        The stripped query.

    Raises:
        ValueError: If the query is empty, not a SELECT/WITH, or multi-statement.
    """
    if not isinstance(sql, str) or not sql.strip():
        raise ValueError(f"`sql` must be a non-empty query string (got {sql!r}).")

    query = sql.strip()
    body = _LEADING_NOISE_RE.sub("", query).lstrip()
    first_word = (body.split(None, 1) or [""])[0].lower()
    if first_word not in ("select", "with"):
        raise ValueError(
            f"Only read-only SELECT/WITH queries are allowed (got a query starting with "
            f"{first_word!r})."
        )

    # A trailing semicolon is fine; anything after it is a second statement.
    stripped = query.rstrip().rstrip(";")
    if ";" in stripped:
        raise ValueError("Only one SQL statement per call — remove the extra ';'.")
    return query


def is_terminal(status: Any) -> bool:
    """Return True when a job status means the job has stopped running."""
    return isinstance(status, str) and status.strip().lower() in TERMINAL_STATUSES


def has_download(job: Any) -> bool:
    """Return True when a job payload carries a download link."""
    if not isinstance(job, dict):
        return False
    link = job.get("link")
    if isinstance(link, dict):
        return bool(link.get("url"))
    return bool(link)


def poll_job(
    path: str,
    *,
    wait_seconds: int,
    fetch: Callable[..., Any] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Poll a job endpoint until it finishes or the wait budget runs out.

    Args:
        path: The job status path.
        wait_seconds: Budget in seconds (already clamped by the caller).
        fetch: Injected request function.
        sleep: Injected sleep, for tests.

    Returns:
        The most recent job payload.
    """
    fetch = fetch or execute
    job = unwrap(fetch("GET", path))
    if wait_seconds <= 0:
        return job if isinstance(job, dict) else {}

    deadline = time.monotonic() + wait_seconds
    while not (is_terminal(job.get("status")) or has_download(job)):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        sleep(min(POLL_INTERVAL, remaining))
        job = unwrap(fetch("GET", path))
        if not isinstance(job, dict):
            return {}
    return job if isinstance(job, dict) else {}


def find_learner_progress_report_id(*, fetch: Callable[..., Any] | None = None) -> str:
    """Return the id of the built-in learner-progress report.

    Raises:
        ValueError: If the instance exposes no learner-progress report.
    """
    fetch = fetch or execute
    reports = unwrap(fetch("GET", "/api/v0/reports"))
    if isinstance(reports, list):
        for report in reports:
            if isinstance(report, dict) and report.get("id") == "learner-progress":
                return "learner-progress"
        if reports and isinstance(reports[0], dict) and reports[0].get("id"):
            return str(reports[0]["id"])
    raise ValueError(
        "This Sana instance exposes no reports. Use sana_run_insights_query for "
        "custom analytics instead."
    )
