"""Response trimming: caps, truncation, and sparse output."""

from __future__ import annotations

from sana_mcp import views


def test_truncate_marks_cut_text():
    assert views.truncate("short") == "short"
    long_text = "x" * (views.MAX_DESCRIPTION + 50)
    trimmed = views.truncate(long_text)
    assert len(trimmed) == views.MAX_DESCRIPTION + 1  # includes the ellipsis
    assert trimmed.endswith("…")


def test_truncate_ignores_blank_and_non_text():
    assert views.truncate("   ") is None
    assert views.truncate(None) is None
    assert views.truncate(42) is None


def test_course_view_keeps_useful_fields_and_drops_noise():
    view = views.course_view(
        {
            "id": "c1",
            "title": "Agile Project Management",
            "description": "Agile is an approach...",
            "imageUrl": "https://img",
            "durationMinutes": 15,
            "type": "live",
            "tags": ["agile"],
            "link": "https://example.com/course",
            "level": None,
            "contentAttributes": {"customAttributes": []},
        }
    )
    assert view["id"] == "c1"
    assert view["link"] == "https://example.com/course"
    assert "imageUrl" not in view
    assert "contentAttributes" not in view
    assert "level" not in view  # None values are omitted


def test_path_view_resolves_courses_and_flags_truncation():
    contents = [f"c{i}" for i in range(views.MAX_PATH_COURSES + 5)]
    courses = [{"id": cid} for cid in contents]
    view = views.path_view({"id": "p1", "title": "Onboarding", "contents": contents}, courses)
    assert view["courseCount"] == len(contents)
    assert len(view["courses"]) == views.MAX_PATH_COURSES
    assert view["coursesTruncated"] is True


def test_path_view_without_resolution_lists_course_ids():
    view = views.path_view({"id": "p1", "title": "P", "contents": ["c1", "c2"]})
    assert view["courseIds"] == ["c1", "c2"]
    assert "courses" not in view


def test_program_view_maps_name_to_title():
    view = views.program_view({"id": "pr1", "name": "Sales program", "selfEnrollmentEnabled": True})
    assert view["title"] == "Sales program"
    assert view["selfEnrollment"] is True


def test_user_view_composes_a_display_name():
    view = views.user_view({"id": "u1", "firstName": "Priya", "lastName": "Patel"})
    assert view["name"] == "Priya Patel"


def test_job_view_flattens_the_download_link():
    view = views.job_view(
        {
            "jobId": "j1",
            "status": "successful",
            "link": {"url": "https://download", "expiresAt": "2026-09-04T13:41:31Z"},
        }
    )
    assert view["downloadUrl"] == "https://download"
    assert view["expiresAt"] == "2026-09-04T13:41:31Z"
    assert "expires" in view["note"]


def test_job_view_omits_note_while_pending():
    view = views.job_view({"jobId": "j1", "status": "pending", "link": None})
    assert view["status"] == "pending"
    assert "downloadUrl" not in view
    assert "note" not in view


def test_view_list_applies_the_cap():
    items = [{"id": f"u{i}", "email": f"u{i}@x.com"} for i in range(views.MAX_LIST_ITEMS + 20)]
    assert len(views.view_list(items, views.user_view)) == views.MAX_LIST_ITEMS
    assert len(views.view_list(items, views.user_view, 5)) == 5
    assert views.view_list("not-a-list", views.user_view) == []
