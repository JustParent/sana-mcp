"""Smoke tests: the server imports, registers every tool, and builds valid schemas.

No network or credentials are touched — the HTTP client is only constructed
inside tool bodies, never at import or during tool listing.
"""

from __future__ import annotations

import asyncio

import pytest

from sana_mcp import server

EXPECTED_TOOLS = {
    # Connection
    "sana_check_connection",
    # Content discovery
    "sana_search_content",
    "sana_get_content",
    # People (read)
    "sana_find_user",
    "sana_get_user",
    "sana_get_user_assignments",
    "sana_list_groups",
    "sana_get_group_members",
    "sana_list_teamspaces",
    # Assign and enroll
    "sana_assign_content",
    "sana_unassign_content",
    "sana_enroll_program_members",
    "sana_unenroll_program_members",
    "sana_add_group_members",
    "sana_remove_group_members",
    "sana_mark_course_completed",
    "sana_reset_course_progress",
    # User administration
    "sana_create_user",
    "sana_update_user",
    "sana_invite_user",
    "sana_delete_user",
    # Group administration
    "sana_create_group",
    "sana_update_group",
    "sana_delete_group",
    # Program administration
    "sana_create_program",
    "sana_update_program",
    "sana_delete_program",
    # Course administration
    "sana_create_course",
    "sana_update_course",
    "sana_upsert_link_courses",
    "sana_delete_course",
    # Teamspace administration
    "sana_create_teamspace",
    "sana_update_teamspace_members",
    "sana_delete_teamspace",
    # Analytics and reporting
    "sana_run_insights_query",
    "sana_run_learner_progress_report",
    "sana_get_report_job",
    # Escape hatch
    "sana_api_request",
}


def _list_tools():
    return asyncio.run(server.mcp.list_tools())


def _fn(tool):
    """Return the plain function behind a registered tool.

    Some FastMCP versions wrap the function and expose it as ``.fn``; others
    return the function itself from the decorator.
    """
    return getattr(tool, "fn", tool)


def test_all_tools_registered():
    names = {tool.name for tool in _list_tools()}
    missing = EXPECTED_TOOLS - names
    unexpected = names - EXPECTED_TOOLS
    assert not missing, f"missing tools: {missing}"
    assert not unexpected, f"unexpected tools: {unexpected}"


def test_tool_count_stays_under_client_catalog_caps():
    # Some MCP hosts only surface the first 40 tools.
    assert len(_list_tools()) <= 40


def test_tools_have_schema_and_description():
    for tool in _list_tools():
        assert tool.description, f"{tool.name} has no description"
        assert isinstance(tool.inputSchema, dict)
        assert tool.inputSchema.get("type") == "object"


def test_descriptions_document_api_cost():
    for tool in _list_tools():
        first_line = tool.description.strip().splitlines()[0]
        assert first_line.endswith(")"), f"{tool.name} does not state its API-call cost"
        assert "API call" in first_line, f"{tool.name} does not state its API-call cost"


def test_destructive_tools_say_so():
    descriptions = {tool.name: tool.description for tool in _list_tools()}
    for name in (
        "sana_delete_user",
        "sana_delete_group",
        "sana_delete_course",
        "sana_delete_program",
        "sana_delete_teamspace",
        "sana_reset_course_progress",
        "sana_unassign_content",
    ):
        assert "estructive" in descriptions[name], f"{name} does not warn that it is destructive"


def test_api_request_validates_method_before_calling():
    with pytest.raises(ValueError, match="method"):
        _fn(server.sana_api_request)(method="TRACE", path="/api/v0/users")


def test_api_request_validates_path_before_calling():
    with pytest.raises(ValueError, match="path"):
        _fn(server.sana_api_request)(method="GET", path="api/v0/users")


def test_search_content_rejects_unknown_content_types():
    with pytest.raises(ValueError, match="content_types"):
        _fn(server.sana_search_content)(query="x", content_types="course,video")


def test_get_content_rejects_unknown_content_type():
    with pytest.raises(ValueError, match="content_type"):
        _fn(server.sana_get_content)(content_type="video", content_id="c1")


def test_bulk_upsert_enforces_batch_size():
    too_many = [{"externalId": str(i)} for i in range(101)]
    with pytest.raises(ValueError, match="At most"):
        _fn(server.sana_upsert_link_courses)(courses=too_many)


def test_bulk_upsert_requires_external_ids():
    with pytest.raises(ValueError, match="externalId"):
        _fn(server.sana_upsert_link_courses)(courses=[{"title": "No external id"}])


def test_assign_content_requires_content_ids():
    with pytest.raises(ValueError, match="content_ids"):
        _fn(server.sana_assign_content)(user="a@b.com", content_ids=[])


def test_update_user_rejects_conflicting_manager_flags():
    with pytest.raises(ValueError, match="not both"):
        _fn(server.sana_update_user)(user="u1", manager="m@x.com", remove_manager=True)


def test_teamspace_members_requires_work():
    with pytest.raises(ValueError, match="nothing to do"):
        _fn(server.sana_update_teamspace_members)(teamspace_id="t1")


def test_create_course_requires_link_for_link_courses():
    with pytest.raises(ValueError, match="link"):
        _fn(server.sana_create_course)(title="Course", course_type="Link")


def test_insights_query_rejects_write_sql():
    with pytest.raises(ValueError, match="read-only"):
        _fn(server.sana_run_insights_query)(sql="DROP TABLE users")
