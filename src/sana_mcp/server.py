"""FastMCP server exposing the Sana learning platform API.

Design principles:

* **Jobs, not endpoints.** Where a real task spans several calls — look up a
  person by email then read their assignments, fetch a path then resolve the
  courses inside it — one tool does the whole job.
* **Grounding first.** Sana has no search API, so ``sana_search_content`` fetches
  the catalog once, caches it briefly, and ranks locally. It is the entry point
  for answering questions from real company learning content.
* **Bounded cost.** Every tool documents its API-call count and returns trimmed
  output, so an LLM driving it has a predictable, finite budget.
* **Nothing unreachable.** ``sana_api_request`` exposes the raw API for the
  endpoints that do not deserve a dedicated tool.
"""

from __future__ import annotations

from typing import Annotated, Any

from mcp.server.fastmcp import FastMCP
from pydantic import Field

from . import reports as reports_mod
from . import search as search_mod
from . import users as users_mod
from . import views
from .client import ALLOWED_METHODS, execute, get_paginated, unwrap

mcp = FastMCP("sana")


def _require_list(value: Any, name: str) -> list[Any]:
    """Validate that a parameter is a non-empty list."""
    if not isinstance(value, list) or not value:
        raise ValueError(f"`{name}` must be a non-empty list (got {value!r}).")
    return value


def _one_of(value: str, allowed: tuple[str, ...], name: str) -> str:
    """Validate an enum-ish string parameter, case-insensitively."""
    if not isinstance(value, str) or value.strip().lower() not in {a.lower() for a in allowed}:
        raise ValueError(f"`{name}` must be one of [{', '.join(allowed)}] (got {value!r}).")
    for candidate in allowed:
        if candidate.lower() == value.strip().lower():
            return candidate
    return value


# --------------------------------------------------------------------------- #
# Connection
# --------------------------------------------------------------------------- #


@mcp.tool()
def sana_check_connection() -> dict[str, Any]:
    """Verify that the Sana credentials work and the API is reachable. (2 API calls.)

    Run this first when setting up the integration: it requests an access token
    and makes one trivial read, so a configuration problem surfaces immediately
    with the exact environment variable to fix.

    Returns:
        ``{ok, baseUrl, scope}``.
    """
    from .auth import resolve_base_url, scope

    execute("GET", "/api/v0/users", params={"limit": 1})
    return {"ok": True, "baseUrl": resolve_base_url(), "scope": scope()}


# --------------------------------------------------------------------------- #
# Content discovery (grounding)
# --------------------------------------------------------------------------- #


@mcp.tool()
def sana_search_content(
    query: str | None = None,
    content_types: Annotated[
        str, Field(description="Comma-separated subset of: course, path, program")
    ] = "course,path,program",
    tags: list[str] | None = None,
    limit: int = 25,
    refresh: bool = False,
) -> dict[str, Any]:
    """Search the Sana catalog to ground an answer in real learning content. (0-15 API calls; cached ~15 min.)

    The Sana API has no search endpoint, so this fetches the catalog once, caches
    it in-process for about fifteen minutes, and ranks locally: a title match
    beats a tag match, which beats a description match. The first search of a
    session pays for the fetch — a few seconds on a large catalog — and later
    ones are served from memory.

    Use it whenever a question is about what training exists ("do we have
    anything on giving feedback?"), before recommending or assigning content, and
    to get the ids that the other tools take. Cite the returned ``link`` when one
    is present; never invent course names.

    Common recipes:

    * What do we offer on a topic: ``query="leadership"``.
    * Browse everything: no ``query`` — returns the catalog alphabetically.
    * Only paths: ``content_types="path"``.
    * Freshly published content missing: ``refresh=True`` to bypass the cache.

    Args:
        query: Free-text search. Omit to browse alphabetically.
        content_types: Which kinds of content to include.
        tags: Keep only content carrying at least one of these tags.
        limit: Maximum results, capped at 50.
        refresh: Refetch the catalog instead of using the cache.

    Returns:
        ``{results: [{id, title, description, contentType, tags?, link?, score?}],
        count, catalogTruncated?, truncatedTypes?, cacheRefreshed?}``.
        ``truncatedTypes`` names content types whose catalog was cut short, so
        results for those may be incomplete.
    """
    types = search_mod.parse_content_types(content_types)
    capped = max(1, min(int(limit), views.MAX_SEARCH_RESULTS))

    items, truncated_types = search_mod.get_catalog(types, refresh=bool(refresh))
    items = search_mod.filter_by_tags(items, tags)
    results = search_mod.rank_items(items, query, capped)

    payload: dict[str, Any] = {"results": results, "count": len(results)}
    if truncated_types:
        payload["catalogTruncated"] = True
        payload["truncatedTypes"] = truncated_types
    if refresh:
        payload["cacheRefreshed"] = True
    return payload


@mcp.tool()
def sana_get_content(
    content_type: Annotated[str, Field(description="course, path, or program")],
    content_id: str,
    include_courses: bool = True,
    raw: bool = False,
) -> dict[str, Any]:
    """Get the full details of one course, path, or program. (1-6 API calls; catalog cached.)

    For a path this also resolves the courses it contains into summaries, which
    is the usual follow-up to ``sana_search_content`` when someone asks what a
    programme actually covers. Those summaries come from the shared content
    catalog: free when it is warm (the usual case, straight after a search),
    but a cold catalog costs a fetch of up to five pages.

    Args:
        content_type: ``course``, ``path``, or ``program``.
        content_id: The content id.
        include_courses: For paths, resolve contained course ids to summaries.
        raw: Return the untrimmed API record.

    Returns:
        A trimmed content record; for paths with ``include_courses`` a
        ``courses`` list, with unresolved ids marked ``{id, missing: true}``.
    """
    kind = _one_of(content_type, search_mod.CONTENT_TYPES, "content_type")
    if not content_id or not str(content_id).strip():
        raise ValueError(f"`content_id` must be a non-empty id (got {content_id!r}).")

    paths = {
        "course": "/api/v0/courses",
        "path": "/api/v0/paths",
        "program": "/api/v0/programs",
    }
    record = unwrap(execute("GET", f"{paths[kind]}/{content_id}"))
    if raw:
        return record if isinstance(record, dict) else {"data": record}

    if kind == "course":
        return views.course_view(record)
    if kind == "program":
        return views.program_view(record)

    contents = record.get("contents") if isinstance(record, dict) else None
    if not include_courses or not isinstance(contents, list):
        return views.path_view(record)

    wanted = [str(cid) for cid in contents[: views.MAX_PATH_COURSES]]
    found = search_mod.find_courses(wanted)
    courses = [found.get(cid) or {"id": cid, "missing": True} for cid in wanted]
    return views.path_view(record, courses)


# --------------------------------------------------------------------------- #
# People (read)
# --------------------------------------------------------------------------- #


@mcp.tool()
def sana_find_user(query: str, limit: int = 10) -> dict[str, Any]:
    """Find Sana users by email address or name. (1-3 API calls.)

    An exact email is a direct lookup. A name is matched locally against a
    bounded scan of the directory, so ``searchTruncated`` in the result means
    there may be further matches beyond the pages scanned.

    Args:
        query: Email address, or part of a name.
        limit: Maximum candidates to return.

    Returns:
        ``{users: [...], count, searchTruncated?}``.
    """
    if not query or not str(query).strip():
        raise ValueError(f"`query` must be a non-empty email or name (got {query!r}).")

    capped = max(1, min(int(limit), views.MAX_LIST_ITEMS))
    if users_mod.classify_identifier(query) == "email":
        record = users_mod.lookup_by_email(query.strip())
        found = [views.user_view(record)] if record else []
        return {"users": found, "count": len(found)}

    matches, truncated = users_mod.search_users_by_name(query, capped)
    payload: dict[str, Any] = {
        "users": [views.user_view(m) for m in matches],
        "count": len(matches),
    }
    if truncated:
        payload["searchTruncated"] = True
    return payload


@mcp.tool()
def sana_get_user(user: str, include_groups: bool = True) -> dict[str, Any]:
    """Get one user's profile and the groups they belong to. (1-3 API calls.)

    Args:
        user: Email address or user id.
        include_groups: Also fetch group memberships.

    Returns:
        ``{user: {...}, groups?: [...]}``.
    """
    user_id = users_mod.resolve_user_id(user)
    record = unwrap(execute("GET", f"/api/v0/users/{user_id}"))
    payload: dict[str, Any] = {"user": views.user_view(record)}

    if include_groups:
        groups = unwrap(execute("GET", f"/api/v0/users/{user_id}/groups", params={"limit": 100}))
        payload["groups"] = views.view_list(groups, views.group_view, views.MAX_GROUPS_PER_USER)
    return payload


@mcp.tool()
def sana_get_user_assignments(user: str, content_type: str | None = None) -> dict[str, Any]:
    """List everything assigned to a user — what is on their plate. (1-2 API calls.)

    Args:
        user: Email address or user id.
        content_type: Optional filter, e.g. ``course`` or ``path``.

    Returns:
        ``{userId, assignments: [{contentId, contentType, title, dueDate,
        status, progress}], count}``.
    """
    user_id = users_mod.resolve_user_id(user)
    payload = execute(
        "GET",
        f"/api/v0/users/{user_id}/assignments",
        params={"contentType": content_type},
    )
    items = unwrap(payload)
    trimmed = views.view_list(items, views.assignment_view, views.MAX_ASSIGNMENTS)
    return {"userId": user_id, "assignments": trimmed, "count": len(trimmed)}


@mcp.tool()
def sana_list_groups(limit: int = 100, cursor: str | None = None) -> dict[str, Any]:
    """List the groups in the Sana instance. (1 API call.)

    Args:
        limit: Page size, capped at 200.
        cursor: ``nextCursor`` from a previous call.

    Returns:
        ``{groups: [...], count, nextCursor?}``.
    """
    from .client import next_cursor

    capped = max(1, min(int(limit), views.MAX_LIST_ITEMS))
    payload = execute("GET", "/api/v0/groups", params={"limit": capped, "next": cursor})
    items = views.view_list(unwrap(payload), views.group_view, capped)
    result: dict[str, Any] = {"groups": items, "count": len(items)}
    nxt = next_cursor(payload)
    if nxt:
        result["nextCursor"] = nxt
    return result


@mcp.tool()
def sana_get_group_members(
    group_id: str, limit: int = 100, cursor: str | None = None
) -> dict[str, Any]:
    """List the members of a group. (1 API call.)

    Args:
        group_id: The group id.
        limit: Page size, capped at 200.
        cursor: ``nextCursor`` from a previous call.

    Returns:
        ``{groupId, members: [...], count, nextCursor?}``.
    """
    from .client import next_cursor

    capped = max(1, min(int(limit), views.MAX_LIST_ITEMS))
    payload = execute(
        "GET", f"/api/v0/groups/{group_id}/users", params={"limit": capped, "next": cursor}
    )
    items = views.view_list(unwrap(payload), views.user_view, capped)
    result: dict[str, Any] = {"groupId": group_id, "members": items, "count": len(items)}
    nxt = next_cursor(payload)
    if nxt:
        result["nextCursor"] = nxt
    return result


@mcp.tool()
def sana_list_teamspaces(
    teamspace_id: str | None = None, include_members: bool = False
) -> dict[str, Any]:
    """List teamspaces, or get one teamspace with its members. (1-2 API calls.)

    Args:
        teamspace_id: Fetch just this teamspace instead of listing all.
        include_members: When fetching one teamspace, also list its members.

    Returns:
        ``{teamspaces: [...], count}`` or ``{teamspace: {...}, members?: [...]}``.
    """
    if not teamspace_id:
        payload = execute("GET", "/api/v0/teamspaces", params={"limit": views.MAX_LIST_ITEMS})
        items = views.view_list(unwrap(payload), views.teamspace_view)
        return {"teamspaces": items, "count": len(items)}

    record = unwrap(execute("GET", f"/api/v0/teamspaces/{teamspace_id}"))
    result: dict[str, Any] = {"teamspace": views.teamspace_view(record)}
    if include_members:
        members = unwrap(execute("GET", f"/api/v0/teamspaces/{teamspace_id}/members"))
        result["members"] = views.view_list(members, views.user_view)
    return result


# --------------------------------------------------------------------------- #
# Assign and enroll (write)
# --------------------------------------------------------------------------- #


@mcp.tool()
def sana_assign_content(
    user: str,
    content_ids: list[str],
    due_date: str | None = None,
    avoid_notifications: bool = False,
) -> dict[str, Any]:
    """Assign courses, paths, or programs to a user. (1-2 API calls.)

    This is a write operation: the learner sees the content on their home page
    and is notified unless ``avoid_notifications`` is set.

    Args:
        user: Email address or user id.
        content_ids: Content ids from ``sana_search_content``.
        due_date: Absolute due date, e.g. ``"2026-09-30T00:00:00Z"``.
        avoid_notifications: Suppress the assignment email.

    Returns:
        ``{userId, assigned: [...], count}``.
    """
    ids = _require_list(content_ids, "content_ids")
    user_id = users_mod.resolve_user_id(user)
    body: dict[str, Any] = {"assignments": [{"id": cid} for cid in ids]}
    if due_date:
        body["dueDateAbsolute"] = due_date
    if avoid_notifications:
        body["avoidNotifications"] = True

    execute("POST", f"/api/v0/users/{user_id}/assignments", json_body=body)
    return {"userId": user_id, "assigned": ids, "count": len(ids)}


@mcp.tool()
def sana_unassign_content(user: str, content_ids: list[str]) -> dict[str, Any]:
    """Remove content assignments from a user. (1-2 API calls.)

    Destructive: the learner loses the assignment and any due date attached to
    it. Completion history recorded against the content is not affected.

    Args:
        user: Email address or user id.
        content_ids: Content ids to unassign.

    Returns:
        ``{userId, unassigned: [...], count}``.
    """
    ids = _require_list(content_ids, "content_ids")
    user_id = users_mod.resolve_user_id(user)
    execute(
        "DELETE",
        f"/api/v0/users/{user_id}/assignments",
        json_body={"assignments": [{"id": cid} for cid in ids]},
    )
    return {"userId": user_id, "unassigned": ids, "count": len(ids)}


@mcp.tool()
def sana_enroll_program_members(
    program_id: str,
    users: list[str],
    available_at: str | None = None,
    required: bool = False,
) -> dict[str, Any]:
    """Enroll users into a program. (1-21 API calls.)

    This is a write operation. Accepts up to 20 emails or user ids per call.

    Args:
        program_id: The program id.
        users: Email addresses or user ids.
        available_at: When the program opens, e.g. ``"2026-09-01T00:00:00Z"``.
        required: Mark the program as required for these learners.

    Returns:
        ``{programId, enrolled: [...], count}``.
    """
    user_ids = users_mod.resolve_user_ids(_require_list(users, "users"))
    memberships = []
    for user_id in user_ids:
        membership: dict[str, Any] = {"userId": user_id, "required": bool(required)}
        if available_at:
            membership["availableAt"] = available_at
        memberships.append(membership)

    execute(
        "POST", f"/api/v0/programs/{program_id}/members", json_body={"memberships": memberships}
    )
    return {"programId": program_id, "enrolled": user_ids, "count": len(user_ids)}


@mcp.tool()
def sana_unenroll_program_members(program_id: str, users: list[str]) -> dict[str, Any]:
    """Remove users from a program. (1-21 API calls.)

    Destructive: enrolled learners lose access to the program's content.

    Args:
        program_id: The program id.
        users: Email addresses or user ids (max 20).

    Returns:
        ``{programId, unenrolled: [...], count}``.
    """
    user_ids = users_mod.resolve_user_ids(_require_list(users, "users"))
    execute(
        "DELETE", f"/api/v0/programs/{program_id}/members", json_body={"userIds": user_ids}
    )
    return {"programId": program_id, "unenrolled": user_ids, "count": len(user_ids)}


@mcp.tool()
def sana_add_group_members(
    group_id: str,
    users: list[str],
    role: Annotated[str, Field(description="learner or group-admin")] = "learner",
) -> dict[str, Any]:
    """Add users to a group. (1-21 API calls.)

    This is a write operation. Group membership usually drives what content
    people are assigned, so adding someone can grant them access.

    Args:
        group_id: The group id.
        users: Email addresses or user ids (max 20).
        role: ``learner`` or ``group-admin``.

    Returns:
        ``{groupId, added: [...], role, count}``.
    """
    member_role = _one_of(role, ("learner", "group-admin"), "role")
    user_ids = users_mod.resolve_user_ids(_require_list(users, "users"))
    execute(
        "POST",
        f"/api/v0/groups/{group_id}/users",
        json_body=[{"id": user_id, "role": member_role} for user_id in user_ids],
    )
    return {"groupId": group_id, "added": user_ids, "role": member_role, "count": len(user_ids)}


@mcp.tool()
def sana_remove_group_members(group_id: str, users: list[str]) -> dict[str, Any]:
    """Remove users from a group. (Up to 40 API calls; max 20 users.)

    Destructive: members may lose access to content granted through the group.
    Sana has no bulk removal endpoint, so this issues one request per user and
    reports each outcome separately.

    Args:
        group_id: The group id.
        users: Email addresses or user ids (max 20).

    Returns:
        ``{groupId, removed: [...], failed: [{userId, error}], count}``.
    """
    user_ids = users_mod.resolve_user_ids(_require_list(users, "users"))
    removed: list[str] = []
    failed: list[dict[str, str]] = []

    for user_id in user_ids:
        try:
            execute("DELETE", f"/api/v0/groups/{group_id}/users/{user_id}")
            removed.append(user_id)
        except Exception as exc:  # noqa: BLE001 - report per-user, keep going.
            failed.append({"userId": user_id, "error": str(exc)})

    result: dict[str, Any] = {"groupId": group_id, "removed": removed, "count": len(removed)}
    if failed:
        result["failed"] = failed
    return result


@mcp.tool()
def sana_mark_course_completed(course_id: str, user: str) -> dict[str, Any]:
    """Mark a course as completed for a user. (1-2 API calls.)

    This is a write operation, typically used to credit training that happened
    outside Sana.

    Args:
        course_id: The course id.
        user: Email address or user id.

    Returns:
        ``{courseId, userId, completed: true}``.
    """
    user_id = users_mod.resolve_user_id(user)
    execute("POST", f"/api/v0/courses/{course_id}/completed/{user_id}")
    return {"courseId": course_id, "userId": user_id, "completed": True}


@mcp.tool()
def sana_reset_course_progress(course_id: str, user: str) -> dict[str, Any]:
    """Reset a user's progress on a course. (1-2 API calls.)

    Destructive and irreversible: the learner's completion and progress for this
    course are erased and cannot be restored. Confirm with the user first.

    Args:
        course_id: The course id.
        user: Email address or user id.

    Returns:
        ``{courseId, userId, reset: true}``.
    """
    user_id = users_mod.resolve_user_id(user)
    execute("POST", f"/api/v0/courses/{course_id}/reset/{user_id}")
    return {"courseId": course_id, "userId": user_id, "reset": True}


# --------------------------------------------------------------------------- #
# User administration
# --------------------------------------------------------------------------- #


@mcp.tool()
def sana_create_user(
    email: str,
    first_name: str | None = None,
    last_name: str | None = None,
    role: str | None = None,
    language: str | None = None,
) -> dict[str, Any]:
    """Create a Sana user. (1 API call.)

    This is a write operation. It does not send an invitation — follow up with
    ``sana_invite_user`` when the person should be emailed.

    Args:
        email: The new user's email address.
        first_name: Given name.
        last_name: Family name.
        role: Platform role, e.g. ``learner`` or ``admin``.
        language: Language code, e.g. ``en``.

    Returns:
        The created user record.
    """
    if not email or "@" not in str(email):
        raise ValueError(f"`email` must be a valid email address (got {email!r}).")

    body = {
        k: v
        for k, v in {
            "email": email,
            "firstName": first_name,
            "lastName": last_name,
            "role": role,
            "language": language,
        }.items()
        if v is not None
    }
    record = unwrap(execute("POST", "/api/v0/users", json_body=body))
    return {"user": views.user_view(record)}


@mcp.tool()
def sana_update_user(
    user: str,
    first_name: str | None = None,
    last_name: str | None = None,
    role: str | None = None,
    language: str | None = None,
    disabled: bool | None = None,
    manager: str | None = None,
    remove_manager: bool = False,
) -> dict[str, Any]:
    """Update a user's profile, status, or manager. (1-4 API calls.)

    This is a write operation covering the whole "change this person's record"
    job: profile fields, deactivation, and the reporting line, which Sana exposes
    as separate endpoints.

    Setting ``disabled=True`` deactivates the account without deleting it — the
    reversible alternative to ``sana_delete_user``.

    Args:
        user: Email address or user id.
        first_name: New given name.
        last_name: New family name.
        role: New platform role.
        language: New language code.
        disabled: True to deactivate, False to reactivate.
        manager: Email or id of the new manager.
        remove_manager: True to clear the existing manager.

    Returns:
        ``{userId, updated: [...fields], user?}``.
    """
    if manager and remove_manager:
        raise ValueError("Pass either `manager` or `remove_manager`, not both.")

    user_id = users_mod.resolve_user_id(user)
    fields = {
        k: v
        for k, v in {
            "firstName": first_name,
            "lastName": last_name,
            "role": role,
            "language": language,
            "disabled": disabled,
        }.items()
        if v is not None
    }

    updated: list[str] = []
    record: Any = None
    if fields:
        record = unwrap(execute("PATCH", f"/api/v0/users/{user_id}", json_body=fields))
        updated.extend(fields)

    if manager:
        manager_id = users_mod.resolve_user_id(manager)
        execute("PUT", f"/api/v0/users/{user_id}/manager/{manager_id}")
        updated.append("manager")
    elif remove_manager:
        execute("DELETE", f"/api/v0/users/{user_id}/manager")
        updated.append("manager")

    if not updated:
        raise ValueError("Nothing to update — pass at least one field to change.")

    result: dict[str, Any] = {"userId": user_id, "updated": updated}
    if record:
        result["user"] = views.user_view(record)
    return result


@mcp.tool()
def sana_invite_user(
    user: str,
    method: Annotated[str, Field(description="email or link")] = "email",
) -> dict[str, Any]:
    """Invite a user to Sana by email, or generate an invite link. (1-2 API calls.)

    This is a write operation.

    Args:
        user: Email address or user id.
        method: ``email`` sends the invitation; ``link`` returns a fresh invite
            link to share yourself.

    Returns:
        ``{userId, method, inviteLink?}``.
    """
    how = _one_of(method, ("email", "link"), "method")
    user_id = users_mod.resolve_user_id(user)

    if how == "email":
        execute("POST", f"/api/v0/users/{user_id}/send-invite")
        return {"userId": user_id, "method": "email", "sent": True}

    payload = unwrap(execute("POST", f"/api/v0/users/{user_id}/invite-link"))
    link = payload.get("link") or payload.get("url") if isinstance(payload, dict) else None
    return {"userId": user_id, "method": "link", "inviteLink": link}


@mcp.tool()
def sana_delete_user(user: str) -> dict[str, Any]:
    """Delete a Sana user. (1-2 API calls.)

    Destructive and irreversible: the account and its learning history are
    removed. Prefer ``sana_update_user(disabled=True)`` to deactivate someone
    reversibly. Confirm with the user before calling this.

    Args:
        user: Email address or user id.

    Returns:
        ``{userId, deleted: true}``.
    """
    user_id = users_mod.resolve_user_id(user)
    execute("DELETE", f"/api/v0/users/{user_id}")
    return {"userId": user_id, "deleted": True}


# --------------------------------------------------------------------------- #
# Group administration
# --------------------------------------------------------------------------- #


@mcp.tool()
def sana_create_group(
    name: str,
    group_type: Annotated[
        str, Field(description="user_manual, user, or program")
    ] = "user_manual",
) -> dict[str, Any]:
    """Create a group. (1 API call.)

    This is a write operation.

    Args:
        name: Group name.
        group_type: ``user_manual`` for a hand-curated group (the usual choice).

    Returns:
        The created group record.
    """
    if not name or not str(name).strip():
        raise ValueError(f"`name` must be a non-empty group name (got {name!r}).")
    kind = _one_of(group_type, ("user_manual", "user", "program"), "group_type")
    record = unwrap(execute("POST", "/api/v0/groups", json_body={"name": name, "type": kind}))
    return {"group": views.group_view(record)}


@mcp.tool()
def sana_update_group(group_id: str, name: str) -> dict[str, Any]:
    """Rename a group. (1 API call.)

    This is a write operation.

    Args:
        group_id: The group id.
        name: The new name.

    Returns:
        The updated group record.
    """
    if not name or not str(name).strip():
        raise ValueError(f"`name` must be a non-empty group name (got {name!r}).")
    record = unwrap(execute("PATCH", f"/api/v0/groups/{group_id}", json_body={"name": name}))
    return {"group": views.group_view(record)}


@mcp.tool()
def sana_delete_group(group_id: str) -> dict[str, Any]:
    """Delete a group. (1 API call.)

    Destructive: members lose any access granted through this group. The users
    themselves are not deleted. Confirm with the user before calling this.

    Args:
        group_id: The group id.

    Returns:
        ``{groupId, deleted: true}``.
    """
    execute("DELETE", f"/api/v0/groups/{group_id}")
    return {"groupId": group_id, "deleted": True}


# --------------------------------------------------------------------------- #
# Program administration
# --------------------------------------------------------------------------- #


@mcp.tool()
def sana_create_program(
    name: str,
    description: str | None = None,
    self_enrollment: bool = False,
    image_url: str | None = None,
) -> dict[str, Any]:
    """Create a program. (1 API call.)

    This is a write operation.

    Args:
        name: Program name.
        description: What the program covers.
        self_enrollment: Let learners enroll themselves.
        image_url: Thumbnail image URL.

    Returns:
        The created program record.
    """
    if not name or not str(name).strip():
        raise ValueError(f"`name` must be a non-empty program name (got {name!r}).")

    body = {
        k: v
        for k, v in {
            "name": name,
            "description": description,
            "selfEnrollmentEnabled": bool(self_enrollment),
            "imageUrl": image_url,
        }.items()
        if v is not None
    }
    record = unwrap(execute("POST", "/api/v0/programs", json_body=body))
    return {"program": views.program_view(record)}


@mcp.tool()
def sana_update_program(
    program_id: str,
    name: str | None = None,
    description: str | None = None,
    self_enrollment: bool | None = None,
    image_url: str | None = None,
) -> dict[str, Any]:
    """Update a program's details. (1 API call.)

    This is a write operation.

    Args:
        program_id: The program id.
        name: New name.
        description: New description.
        self_enrollment: Enable or disable self-enrollment.
        image_url: New thumbnail image URL.

    Returns:
        The updated program record.
    """
    body = {
        k: v
        for k, v in {
            "name": name,
            "description": description,
            "selfEnrollmentEnabled": self_enrollment,
            "imageUrl": image_url,
        }.items()
        if v is not None
    }
    if not body:
        raise ValueError("Nothing to update — pass at least one field to change.")

    record = unwrap(execute("PATCH", f"/api/v0/programs/{program_id}", json_body=body))
    return {"program": views.program_view(record)}


@mcp.tool()
def sana_delete_program(program_id: str) -> dict[str, Any]:
    """Delete a program. (1 API call.)

    Destructive: enrolled learners lose the program. Sana only allows deleting
    programs that were created through the API. Confirm before calling this.

    Args:
        program_id: The program id.

    Returns:
        ``{programId, deleted: true}``.
    """
    execute("DELETE", f"/api/v0/programs/{program_id}")
    return {"programId": program_id, "deleted": True}


# --------------------------------------------------------------------------- #
# Course administration
# --------------------------------------------------------------------------- #


@mcp.tool()
def sana_create_course(
    title: str,
    course_type: Annotated[str, Field(description="Link, SelfPaced, or Live")] = "Link",
    link: str | None = None,
    duration_minutes: int | None = None,
    external_id: str | None = None,
    visibility: str | None = None,
) -> dict[str, Any]:
    """Create a course. (1 API call.)

    This is a write operation. A ``Link`` course points at content hosted
    elsewhere and needs ``link``; ``SelfPaced`` and ``Live`` courses are authored
    in Sana afterwards.

    Args:
        title: Course title.
        course_type: ``Link``, ``SelfPaced``, or ``Live``.
        link: Destination URL, required for ``Link`` courses.
        duration_minutes: Expected duration.
        external_id: Your own identifier, for syncing an external catalog.
        visibility: Course visibility setting.

    Returns:
        The created course record.
    """
    if not title or not str(title).strip():
        raise ValueError(f"`title` must be a non-empty course title (got {title!r}).")
    kind = _one_of(course_type, ("Link", "SelfPaced", "Live"), "course_type")
    if kind == "Link" and not link:
        raise ValueError("`link` is required when course_type is 'Link'.")

    body = {
        k: v
        for k, v in {
            "title": title,
            "courseType": kind,
            "link": link,
            "durationInMinutes": duration_minutes,
            "externalId": external_id,
            "visibility": visibility,
        }.items()
        if v is not None
    }
    record = unwrap(execute("POST", "/api/v1/courses", json_body=body))
    return {"course": views.course_view(record)}


@mcp.tool()
def sana_update_course(
    course_id: str,
    title: str | None = None,
    description: str | None = None,
    link: str | None = None,
    duration_minutes: int | None = None,
    tags: list[str] | None = None,
    reset_duration_to_default: bool = False,
) -> dict[str, Any]:
    """Update a course's details. (1 API call.)

    This is a write operation.

    Args:
        course_id: The course id.
        title: New title.
        description: New description.
        link: New destination URL.
        duration_minutes: New duration.
        tags: Replacement tag list.
        reset_duration_to_default: Clear a custom duration.

    Returns:
        The updated course record.
    """
    body: dict[str, Any] = {
        k: v
        for k, v in {
            "title": title,
            "description": description,
            "link": link,
            "durationInMinutes": duration_minutes,
            "tags": tags,
        }.items()
        if v is not None
    }
    if reset_duration_to_default:
        body["resetDurationToDefault"] = True
    if not body:
        raise ValueError("Nothing to update — pass at least one field to change.")

    record = unwrap(execute("PATCH", f"/api/v0/courses/{course_id}", json_body=body))
    return {"course": views.course_view(record)}


@mcp.tool()
def sana_upsert_link_courses(courses: list[dict[str, Any]]) -> dict[str, Any]:
    """Create or update many link courses in one call, keyed by externalId. (1 API call.)

    This is a write operation and the tool for syncing an external content
    catalog into Sana: each entry needs an ``externalId``, and an existing course
    with that id is updated rather than duplicated.

    Args:
        courses: Up to 100 course objects, each with ``externalId``, ``title``,
            ``link``, and optionally ``description``/``durationInMinutes``.

    Returns:
        ``{upserted: n, result}``.
    """
    batch = _require_list(courses, "courses")
    if len(batch) > views.MAX_BULK_COURSES:
        raise ValueError(
            f"At most {views.MAX_BULK_COURSES} courses per call (got {len(batch)}). "
            "Split the batch."
        )
    missing = [i for i, course in enumerate(batch) if not (course or {}).get("externalId")]
    if missing:
        raise ValueError(f"Every course needs an externalId; missing at positions {missing}.")

    payload = unwrap(execute("POST", "/api/v1/courses/bulk-link-upsert", json_body=batch))
    return {"upserted": len(batch), "result": payload}


@mcp.tool()
def sana_delete_course(course_id: str) -> dict[str, Any]:
    """Delete a course. (1 API call.)

    Destructive: the course and its completion records are removed, including
    for learners who finished it. Confirm with the user before calling this.

    Args:
        course_id: The course id.

    Returns:
        ``{courseId, deleted: true}``.
    """
    execute("DELETE", f"/api/v0/courses/{course_id}")
    return {"courseId": course_id, "deleted": True}


# --------------------------------------------------------------------------- #
# Teamspace administration
# --------------------------------------------------------------------------- #


@mcp.tool()
def sana_create_teamspace(
    name: str,
    is_private: bool = False,
    owner: str | None = None,
    default_role: Annotated[
        str | None, Field(description="viewer, commenter, editor, or owner")
    ] = None,
) -> dict[str, Any]:
    """Create a teamspace. (1-2 API calls.)

    This is a write operation.

    Args:
        name: Teamspace name.
        is_private: Restrict visibility to members.
        owner: Email or user id of the owner.
        default_role: Default role for new members.

    Returns:
        The created teamspace record.
    """
    if not name or not str(name).strip():
        raise ValueError(f"`name` must be a non-empty teamspace name (got {name!r}).")
    if default_role:
        default_role = _one_of(
            default_role, ("viewer", "commenter", "editor", "owner"), "default_role"
        )

    body: dict[str, Any] = {"name": name, "isPrivate": bool(is_private)}
    if owner:
        body["ownerUUID"] = users_mod.resolve_user_id(owner)
    if default_role:
        body["defaultRole"] = default_role

    record = unwrap(execute("POST", "/api/v0/teamspaces", json_body=body))
    return {"teamspace": views.teamspace_view(record)}


@mcp.tool()
def sana_update_teamspace_members(
    teamspace_id: str,
    add: list[str] | None = None,
    remove: list[str] | None = None,
    role: Annotated[
        str, Field(description="viewer, commenter, editor, or owner")
    ] = "viewer",
) -> dict[str, Any]:
    """Add or remove teamspace members. (1-42 API calls; max 20 each way.)

    This is a write operation; removals revoke access to the teamspace's content.

    Args:
        teamspace_id: The teamspace id.
        add: Emails or user ids to add (max 20).
        remove: Emails or user ids to remove (max 20).
        role: Role granted to added members.

    Returns:
        ``{teamspaceId, added: [...], removed: [...]}``.
    """
    if not add and not remove:
        raise ValueError("Pass `add` and/or `remove` — nothing to do otherwise.")
    member_role = _one_of(role, ("viewer", "commenter", "editor", "owner"), "role")

    result: dict[str, Any] = {"teamspaceId": teamspace_id}
    if add:
        added = users_mod.resolve_user_ids(add)
        execute(
            "POST",
            f"/api/v0/teamspaces/{teamspace_id}/members",
            json_body={"usersToAdd": [{"id": uid, "role": member_role} for uid in added]},
        )
        result["added"] = added
        result["role"] = member_role
    if remove:
        removed = users_mod.resolve_user_ids(remove)
        execute(
            "DELETE",
            f"/api/v0/teamspaces/{teamspace_id}/members",
            json_body={"usersToRemove": removed},
        )
        result["removed"] = removed
    return result


@mcp.tool()
def sana_delete_teamspace(teamspace_id: str) -> dict[str, Any]:
    """Delete a teamspace. (1 API call.)

    Destructive: the teamspace and its contents are removed. Sana only allows
    deleting API-managed teamspaces. Confirm before calling this.

    Args:
        teamspace_id: The teamspace id.

    Returns:
        ``{teamspaceId, deleted: true}``.
    """
    execute("DELETE", f"/api/v0/teamspaces/{teamspace_id}")
    return {"teamspaceId": teamspace_id, "deleted": True}


# --------------------------------------------------------------------------- #
# Analytics and reporting
# --------------------------------------------------------------------------- #


@mcp.tool()
def sana_run_insights_query(
    sql: str,
    output_format: Annotated[str, Field(description="csv or xlsx")] = "csv",
    wait_seconds: int = 10,
) -> dict[str, Any]:
    """Run a read-only SQL query against Sana's analytics warehouse. (2-12 API calls.)

    Use this for questions the fixed reports cannot answer — completion rates by
    group, progress over time, and similar. Sana runs the query as a job and
    returns a download link, so the answer arrives as a CSV/XLSX URL rather than
    inline rows.

    The main analytics table is ``"analytics"."user_course_instance_progress"``
    with columns including ``user``, ``course``, ``start_date``,
    ``completion_date``, ``last_progress_date``, ``course_instance``. Build
    queries in the Sana UI (Manage -> Insights) and copy the SQL if unsure.

    Only a single ``SELECT``/``WITH`` statement is accepted. If the job is still
    running when the wait budget expires, poll with ``sana_get_report_job``.

    Args:
        sql: The query, e.g. ``SELECT "user", "course" FROM "analytics"."user_course_instance_progress"``.
        output_format: ``csv`` or ``xlsx``.
        wait_seconds: How long to wait for completion, capped at 20.

    Returns:
        ``{jobId, status, downloadUrl?, expiresAt?, note?}``.
    """
    query = reports_mod.validate_insights_sql(sql)
    fmt = _one_of(output_format, ("csv", "xlsx"), "output_format")
    budget = reports_mod.clamp_wait(wait_seconds)

    created = unwrap(
        execute("POST", "/api/v1/reports/query", json_body={"query": query, "format": fmt})
    )
    job_id = created.get("jobId") or created.get("id") if isinstance(created, dict) else None
    if not job_id:
        return views.job_view(created)

    job = reports_mod.poll_job(f"/api/v1/reports/jobs/{job_id}", wait_seconds=budget)
    return views.job_view(job or created)


@mcp.tool()
def sana_run_learner_progress_report(
    content_types: list[str] | None = None,
    group_ids: list[str] | None = None,
    content_ids: list[str] | None = None,
    assignment_type: str | None = None,
    output_format: Annotated[str, Field(description="csv or xlsx")] = "csv",
    wait_seconds: int = 10,
) -> dict[str, Any]:
    """Run Sana's built-in learner-progress report — who completed what. (3-13 API calls.)

    The quickest route to "how is my team doing on their training". Prefer this
    over ``sana_run_insights_query`` when the standard progress export answers
    the question. Results arrive as a download link.

    Args:
        content_types: Filter, e.g. ``["course", "path"]``.
        group_ids: Restrict to these groups.
        content_ids: Restrict to specific content.
        assignment_type: ``all`` or ``assigned``.
        output_format: ``csv`` or ``xlsx``.
        wait_seconds: How long to wait for completion, capped at 20.

    Returns:
        ``{reportId, jobId, status, downloadUrl?, expiresAt?}``.
    """
    fmt = _one_of(output_format, ("csv", "xlsx"), "output_format")
    budget = reports_mod.clamp_wait(wait_seconds)
    report_id = reports_mod.find_learner_progress_report_id()

    body = {
        k: v
        for k, v in {
            "contentTypes": content_types,
            "groups": group_ids,
            "contentIds": content_ids,
            "assignmentType": assignment_type,
            "outputFormat": fmt,
        }.items()
        if v is not None
    }
    created = unwrap(execute("POST", f"/api/v0/reports/{report_id}/jobs", json_body=body))
    job_id = created.get("jobId") or created.get("id") if isinstance(created, dict) else None
    if not job_id:
        return dict(views.job_view(created), reportId=report_id)

    job = reports_mod.poll_job(
        f"/api/v0/reports/{report_id}/jobs/{job_id}", wait_seconds=budget
    )
    return dict(views.job_view(job or created), reportId=report_id)


@mcp.tool()
def sana_get_report_job(job_id: str, report_id: str | None = None) -> dict[str, Any]:
    """Check a report job and get its download link once it is ready. (1 API call.)

    Call this after ``sana_run_insights_query`` or
    ``sana_run_learner_progress_report`` returns a pending status — re-running
    the report would start the work over.

    Args:
        job_id: The job id from the report tool.
        report_id: Set to ``learner-progress`` for a legacy report job; omit for
            an insights query.

    Returns:
        ``{jobId, status, downloadUrl?, expiresAt?, note?}``.
    """
    if not job_id or not str(job_id).strip():
        raise ValueError(f"`job_id` must be a non-empty job id (got {job_id!r}).")

    path = (
        f"/api/v0/reports/{report_id}/jobs/{job_id}"
        if report_id
        else f"/api/v1/reports/jobs/{job_id}"
    )
    return views.job_view(unwrap(execute("GET", path)))


# --------------------------------------------------------------------------- #
# Escape hatch
# --------------------------------------------------------------------------- #


@mcp.tool()
def sana_api_request(
    method: Annotated[str, Field(description="GET, POST, PATCH, PUT, or DELETE")],
    path: str,
    query: dict[str, Any] | None = None,
    body: dict[str, Any] | None = None,
) -> Any:
    """Call any Sana API endpoint directly (advanced; can modify or delete data). (1 API call.)

    The escape hatch for endpoints without a dedicated tool — the custom
    attributes schema, xAPI statements, paginating past what a list tool returns.
    Responses are returned untrimmed, so prefer the specific tools where one
    exists and keep ``limit`` small.

    Args:
        method: HTTP method.
        path: API path starting with ``/``, e.g. ``/api/v0/users/attributes-schema``.
        query: Query parameters.
        body: JSON request body.

    Returns:
        The raw JSON response.
    """
    verb = _one_of(method, ALLOWED_METHODS, "method")
    if not isinstance(path, str) or not path.startswith("/"):
        raise ValueError(f"`path` must start with '/' (got {path!r}), e.g. '/api/v0/users'.")

    return execute(verb, path, params=query, json_body=body)


def main() -> None:
    """Console entry point: ``sana-mcp`` (stdio transport).

    Configuration is validated first so a missing variable is reported on stderr
    at startup, but the server always starts: individual tool calls surface a
    clear error, which is where an assistant can act on it.
    """
    from .auth import ensure_credentials

    ensure_credentials()
    mcp.run()


if __name__ == "__main__":
    main()
