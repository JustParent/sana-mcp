"""Trim Sana API records into compact, bounded shapes for an LLM.

Tools return sparse dicts: a field is present only when the API supplied a
meaningful value, so an assistant reading the output is not wading through nulls.
Every collection is capped by a ``MAX_*`` constant below, and long descriptions
are truncated rather than silently dropped.

Course ``link`` values are passed through from the API; this module never
synthesizes Sana content URLs, because the tenant URL scheme is not part of the
documented API.
"""

from __future__ import annotations

from typing import Any

MAX_DESCRIPTION = 400
MAX_SEARCH_RESULTS = 50
MAX_LIST_ITEMS = 200
MAX_PATH_COURSES = 50
MAX_ASSIGNMENTS = 100
MAX_GROUPS_PER_USER = 50
MAX_CATALOG_PAGES = 5
MAX_USER_SEARCH_PAGES = 3
MAX_BULK_USERS = 20
MAX_BULK_COURSES = 100


def truncate(text: Any, limit: int = MAX_DESCRIPTION) -> str | None:
    """Shorten free text to ``limit`` characters, marking any cut with an ellipsis."""
    if not isinstance(text, str):
        return None
    cleaned = text.strip()
    if not cleaned:
        return None
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[:limit].rstrip() + "…"


def _sparse(fields: dict[str, Any]) -> dict[str, Any]:
    """Drop keys whose value is None or an empty string/list/dict."""
    return {k: v for k, v in fields.items() if v not in (None, "", [], {})}


def course_view(course: Any) -> dict[str, Any]:
    """Summarize a course record."""
    if not isinstance(course, dict):
        return {}
    return _sparse(
        {
            "id": course.get("id"),
            "title": course.get("title"),
            "description": truncate(course.get("description")),
            "type": course.get("type") or course.get("courseType"),
            "level": course.get("level"),
            "tags": course.get("tags"),
            "durationMinutes": course.get("durationMinutes"),
            "externalId": course.get("externalId"),
            "link": course.get("link"),
        }
    )


def path_view(path: Any, courses: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Summarize a learning path, optionally with its resolved courses."""
    if not isinstance(path, dict):
        return {}
    contents = path.get("contents")
    view = _sparse(
        {
            "id": path.get("id"),
            "title": path.get("title"),
            "description": truncate(path.get("description")),
            "courseCount": len(contents) if isinstance(contents, list) else None,
        }
    )
    if courses is not None:
        view["courses"] = courses[:MAX_PATH_COURSES]
        if isinstance(contents, list) and len(contents) > MAX_PATH_COURSES:
            view["coursesTruncated"] = True
    elif isinstance(contents, list):
        view["courseIds"] = contents[:MAX_PATH_COURSES]
    return view


def program_view(program: Any) -> dict[str, Any]:
    """Summarize a program record."""
    if not isinstance(program, dict):
        return {}
    return _sparse(
        {
            "id": program.get("id"),
            "title": program.get("name") or program.get("title"),
            "description": truncate(program.get("description")),
            "selfEnrollment": program.get("selfEnrollmentEnabled"),
            "link": program.get("link"),
        }
    )


def user_view(user: Any) -> dict[str, Any]:
    """Summarize a user record."""
    if not isinstance(user, dict):
        return {}
    first = user.get("firstName")
    last = user.get("lastName")
    name = user.get("name") or " ".join(p for p in (first, last) if p) or None
    return _sparse(
        {
            "id": user.get("id"),
            "email": user.get("email"),
            "name": name,
            "firstName": first,
            "lastName": last,
            "role": user.get("role"),
            "language": user.get("language"),
            "disabled": user.get("disabled"),
            "createdAt": user.get("createdAt"),
            "managerId": user.get("managerId") or user.get("manager"),
        }
    )


def group_view(group: Any) -> dict[str, Any]:
    """Summarize a group record."""
    if not isinstance(group, dict):
        return {}
    return _sparse(
        {
            "id": group.get("id"),
            "name": group.get("name"),
            "type": group.get("type"),
            "role": group.get("role"),
            "memberCount": group.get("memberCount") or group.get("userCount"),
        }
    )


def teamspace_view(teamspace: Any) -> dict[str, Any]:
    """Summarize a teamspace record."""
    if not isinstance(teamspace, dict):
        return {}
    return _sparse(
        {
            "id": teamspace.get("id") or teamspace.get("teamspaceId"),
            "name": teamspace.get("name"),
            "isPrivate": teamspace.get("isPrivate"),
            "defaultRole": teamspace.get("defaultRole"),
            "ownerId": teamspace.get("ownerUUID") or teamspace.get("ownerId"),
        }
    )


def assignment_view(assignment: Any) -> dict[str, Any]:
    """Summarize an assignment record."""
    if not isinstance(assignment, dict):
        return {}
    return _sparse(
        {
            "contentId": assignment.get("contentId") or assignment.get("id"),
            "contentType": assignment.get("contentType") or assignment.get("type"),
            "title": assignment.get("title"),
            "assignedAt": assignment.get("assignedAt") or assignment.get("createdAt"),
            "dueDate": assignment.get("dueDate") or assignment.get("dueDateAbsolute"),
            "required": assignment.get("required"),
            "status": assignment.get("status"),
            "progress": assignment.get("progress"),
            "completedAt": assignment.get("completionDate") or assignment.get("completedAt"),
        }
    )


def job_view(job: Any) -> dict[str, Any]:
    """Summarize a report job, flattening its download link."""
    if not isinstance(job, dict):
        return {}
    link = job.get("link")
    url = link.get("url") if isinstance(link, dict) else link if isinstance(link, str) else None
    expires_at = link.get("expiresAt") if isinstance(link, dict) else None
    view = _sparse(
        {
            "jobId": job.get("jobId") or job.get("id"),
            "status": job.get("status"),
            "createdAt": job.get("createdAt"),
            "finishedAt": job.get("finishedAt"),
            "downloadUrl": url,
            "expiresAt": expires_at,
        }
    )
    if url:
        view["note"] = "The download link expires; fetch it promptly."
    return view


def view_list(items: Any, viewer, limit: int = MAX_LIST_ITEMS) -> list[dict[str, Any]]:
    """Apply ``viewer`` to each record, capped at ``limit`` entries."""
    if not isinstance(items, list):
        return []
    return [viewer(item) for item in items[:limit]]
