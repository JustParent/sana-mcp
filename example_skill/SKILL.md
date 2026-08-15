---
name: sana-learning
description: Answer questions about company training and manage learning in Sana — find courses and paths to ground an answer, check what someone has been assigned, assign or enroll people, and pull progress reports. Use whenever someone asks what training exists, what they should learn, what is on a person's plate, or asks to assign, enroll, or report on learning.
---

# Working with Sana

Sana is the company learning platform. These recipes cover the jobs people actually ask for. The tools are documented in the MCP server itself — this skill is about which ones to reach for and in what order.

## Grounding an answer in real content

When someone asks what training exists on a topic, **always search before answering**. Never name a course from memory: course titles that sound plausible usually don't exist.

1. `sana_search_content(query="giving feedback")` — returns ranked courses, paths, and programs.
2. `sana_get_content(content_type="path", content_id=…)` — for a path, this resolves the courses inside it, which is what people actually want to know ("what does the manager path cover?").
3. Cite the `link` field when one is present. If nothing matches, say so plainly and offer to search a related term — don't fill the gap with invented content.

Notes:

- Search is local ranking over a cached catalog, so results can be up to fifteen minutes stale. If someone says "I just published it", retry with `refresh=True`.
- The first search of a session fetches the catalog and can take a few seconds; later searches are instant. Search once and reuse the ids.
- `catalogTruncated: true` means the catalog was cut short by the page bound or the fetch budget, and `truncatedTypes` says which types. Results for those types may be incomplete — say so rather than concluding the content doesn't exist, and narrow with `content_types` or `tags`.
- No query at all lists the catalog alphabetically — useful for "what do we have?" browsing.

## What's on someone's plate

1. `sana_get_user_assignments(user="priya@acme.com")` — email or user id both work, no lookup step needed.
2. `sana_get_user(user=…)` if you also need their groups or manager.

Use `sana_find_user(query="Priya")` when you only have a name. Names are matched over a bounded directory scan, so `searchTruncated: true` means there may be more matches — ask for the email to be sure.

## Onboarding someone

The usual sequence:

1. `sana_find_user` to confirm the person exists (or `sana_create_user` then `sana_invite_user` if they don't).
2. `sana_add_group_members(group_id=…, users=[…])` — group membership often drives access.
3. `sana_enroll_program_members(program_id=…, users=[…], available_at=…)` for a structured programme.
4. `sana_assign_content(user=…, content_ids=[…], due_date=…)` for specific courses.
5. `sana_get_user_assignments` to confirm and report back what they'll see.

Batch tools take up to 20 people per call. Split larger lists.

## Reporting on progress

- `sana_run_learner_progress_report(group_ids=[…])` answers most "how is my team doing" questions.
- `sana_run_insights_query(sql=…)` for anything custom. The main table is `"analytics"."user_course_instance_progress"` with columns including `user`, `course`, `start_date`, `completion_date`. Only single read-only `SELECT`/`WITH` statements are accepted.

Both are asynchronous. They poll briefly and may return `status: "pending"` with a `jobId`. **Poll with `sana_get_report_job(job_id=…)` — do not re-run the report**, which starts the work over. Results are a download link that expires, so pass it on promptly.

## Changing things

Write tools change the real platform and learners get notified. Before any write:

- State plainly what you're about to change and to whom, and get agreement.
- Prefer the reversible option: `sana_update_user(disabled=True)` deactivates an account; `sana_delete_user` erases it and its history.

Treat these as requiring explicit confirmation every time: `sana_delete_user`, `sana_delete_group`, `sana_delete_course`, `sana_delete_program`, `sana_delete_teamspace`, `sana_reset_course_progress`, `sana_unassign_content`, `sana_unenroll_program_members`, `sana_remove_group_members`.

`sana_api_request` can call any endpoint including deletes. Use it only when no dedicated tool fits, and confirm first for anything that isn't a `GET`.

## Cost discipline

Each tool's description states its API-call count. Two habits keep runs cheap:

- Search once and reuse the ids, rather than re-searching for each follow-up question.
- Pass user ids instead of emails in batch operations when you already have them — each email costs an extra lookup.

## Troubleshooting

- Every tool failing with a message about `SANA_DOMAIN` or `SANA_CLIENT_ID` means the integration isn't configured; run `sana_check_connection` and report what it says.
- A 403 on a write tool means the API client is read-only.
- Newly published content missing from search: `sana_search_content(refresh=True)`.
