# sana-mcp

A `uvx`-runnable MCP server for the [Sana](https://sana.ai) learning platform API, designed around the jobs people actually ask an assistant to do:

- **Ground answers in real learning content** — search the course, path, and program catalog and cite what exists, instead of inventing training that doesn't.
- **Answer "what's on someone's plate"** — look a person up by email and read their assignments in one step.
- **Assign, enroll, and manage membership** — assign content with due dates, enroll people into programs, curate groups and teamspaces.
- **Administer users, groups, programs, and courses** — create, update, invite, deactivate, and delete.
- **Pull analytics** — run the built-in learner-progress report, or custom SQL against the analytics warehouse.
- **Reach anything else** — a raw `sana_api_request` escape hatch means no endpoint is off-limits.

Every tool makes a **bounded number of API calls** (stated in its description) and returns **trimmed output**, so an LLM driving it has a predictable, finite budget.

## Why search works the way it does

Sana's public API has no search endpoint. To answer "do we have anything on giving feedback?", this server fetches the content catalog (courses, paths, programs), caches it in-process for about five minutes, and ranks it locally: an exact phrase in a title beats a title word, which beats a tag, which beats a description match.

Two consequences worth knowing:

- Search results can be **up to five minutes stale**. Pass `refresh=true` to `sana_search_content` right after publishing content.
- Very large catalogs are capped at 5 pages of 1000 records per content type. When that bound is hit the result carries `catalogTruncated: true`.

## 1. Sana API client setup (one time)

1. In Sana, go to **Settings → API** and create an API client.
2. Copy the **client ID** and **client secret**. The secret is shown once.
3. Note your Sana domain — the subdomain in your Sana URL, e.g. `acme` for `https://acme.sana.ai`.
4. Grant the client `read` scope for read-only use, or `read,write` to allow the write tools.

The server exchanges these for a 1-hour access token automatically and refreshes it in the background; there is no interactive login step.

## 2. Install

```bash
uvx --from git+https://github.com/justparent/sana-mcp sana-mcp
```

> Once published to PyPI you'll be able to drop `--from …`.

## 3. Configure your MCP client

Claude Desktop / Claude Code (`mcpServers` config):

```json
{
  "mcpServers": {
    "sana": {
      "command": "uvx",
      "args": [
        "--from", "git+https://github.com/justparent/sana-mcp",
        "sana-mcp"
      ],
      "env": {
        "SANA_DOMAIN": "acme",
        "SANA_CLIENT_ID": "your-client-id",
        "SANA_CLIENT_SECRET": "your-client-secret"
      }
    }
  }
}
```

After PyPI publication: `"args": ["sana-mcp"]`.

Run `sana_check_connection` first — it verifies credentials and connectivity in two calls and names the exact variable to fix if something is wrong.

### Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `SANA_DOMAIN` | *(required)* | Your Sana subdomain (`acme`), host (`acme.sana.ai`), or full URL (`https://acme.sana.ai`) |
| `SANA_CLIENT_ID` | *(required)* | OAuth2 client ID from Sana's API settings |
| `SANA_CLIENT_SECRET` | *(required)* | OAuth2 client secret |
| `SANA_SCOPE` | `read,write` | Token scope. Set to `read` to deploy read-only — write tools then fail with a clear 403 |
| `SANA_CATALOG_TTL` | `300` | Seconds the content catalog is cached for search |

> `SANA_DOMAIN` also accepts the aliases `SANA_BASE_URL` / `SANA_URL`, and the credentials accept `CLIENT_ID` / `CLIENT_SECRET`. The `SANA_*` names win when both are set.

## 4. Use with Harriet (justparent)

Harriet runs MCP servers as sandboxed stdio subprocesses. Configure the integration with:

```json
{
  "server_type": "sandboxed",
  "sandbox_command": "uvx",
  "sandbox_args": "[\"--from\", \"git+https://github.com/justparent/sana-mcp\", \"sana-mcp\"]",
  "sandbox_runtime": "python",
  "sandbox_timeout": "60",
  "requires_user_oauth": false,
  "auth_type": "none",
  "sandbox_env": {
    "SANA_DOMAIN": "acme",
    "SANA_CLIENT_ID": "your-client-id",
    "SANA_CLIENT_SECRET": "$SECRET_KEY"
  }
}
```

Notes:

- `sandbox_args` must be a **JSON-encoded string**, not a list.
- Put the client secret in the integration's **Secret Key** field; `$SECRET_KEY` is substituted at runtime so the secret is never stored in the config.
- `sandbox_timeout: "60"` gives the report tools headroom. No tool blocks longer than ~25 seconds regardless.
- The server is headless and needs no per-user OAuth — one API client serves the whole workspace, and Sana's own permissions apply.
- Set `requires_confirmation` on the destructive tools (marked below) so a human approves them.

## Tools

### Connection

| Tool | Purpose | API calls |
|---|---|---|
| `sana_check_connection()` | Verify credentials and connectivity | 2 |

### Content discovery (grounding)

| Tool | Purpose | API calls |
|---|---|---|
| `sana_search_content(query?, content_types?, tags?, limit?, refresh?)` | Search/browse the catalog; the entry point for grounding answers | 0–15 (cached) |
| `sana_get_content(content_type, content_id, include_courses?, raw?)` | Full details of a course/path/program; resolves a path's courses | 1–4 |

### People (read)

| Tool | Purpose | API calls |
|---|---|---|
| `sana_find_user(query, limit?)` | Find users by email or name | 1–3 |
| `sana_get_user(user, include_groups?)` | Profile + group memberships | 1–3 |
| `sana_get_user_assignments(user, content_type?)` | What is assigned to someone | 1–2 |
| `sana_list_groups(limit?, cursor?)` | List groups | 1 |
| `sana_get_group_members(group_id, limit?, cursor?)` | List a group's members | 1 |
| `sana_list_teamspaces(teamspace_id?, include_members?)` | List teamspaces, or one with members | 1–2 |

> Anywhere a `user` is accepted, pass an email address **or** a user id — emails cost one extra lookup.

### Assign and enroll (write)

| Tool | Purpose | API calls | Destructive |
|---|---|---|---|
| `sana_assign_content(user, content_ids, due_date?, avoid_notifications?)` | Assign content to a learner | 1–2 | |
| `sana_unassign_content(user, content_ids)` | Remove assignments | 1–2 | ✅ |
| `sana_enroll_program_members(program_id, users, available_at?, required?)` | Enroll up to 20 people | 1–21 | |
| `sana_unenroll_program_members(program_id, users)` | Remove people from a program | 1–21 | ✅ |
| `sana_add_group_members(group_id, users, role?)` | Add up to 20 people to a group | 1–21 | |
| `sana_remove_group_members(group_id, users)` | Remove people from a group | ≤40 | ✅ |
| `sana_mark_course_completed(course_id, user)` | Credit training done elsewhere | 1–2 | |
| `sana_reset_course_progress(course_id, user)` | Wipe a learner's progress | 1–2 | ✅ |

### Administration (write)

| Tool | Purpose | API calls | Destructive |
|---|---|---|---|
| `sana_create_user(email, first_name?, last_name?, role?, language?)` | Create a user | 1 | |
| `sana_update_user(user, …, disabled?, manager?, remove_manager?)` | Update profile, deactivate, set/clear manager | 1–3 | |
| `sana_invite_user(user, method?)` | Send an invite email or mint an invite link | 1–2 | |
| `sana_delete_user(user)` | Delete a user and their history | 1–2 | ✅ |
| `sana_create_group(name, group_type?)` | Create a group | 1 | |
| `sana_update_group(group_id, name)` | Rename a group | 1 | |
| `sana_delete_group(group_id)` | Delete a group | 1 | ✅ |
| `sana_create_program(name, description?, self_enrollment?, image_url?)` | Create a program | 1 | |
| `sana_update_program(program_id, …)` | Update a program | 1 | |
| `sana_delete_program(program_id)` | Delete an API-created program | 1 | ✅ |
| `sana_create_course(title, course_type?, link?, duration_minutes?, external_id?, visibility?)` | Create a course | 1 | |
| `sana_update_course(course_id, …)` | Update a course | 1 | |
| `sana_upsert_link_courses(courses)` | Sync up to 100 link courses by `externalId` | 1 | |
| `sana_delete_course(course_id)` | Delete a course and its completions | 1 | ✅ |
| `sana_create_teamspace(name, is_private?, owner?, default_role?)` | Create a teamspace | 1–2 | |
| `sana_update_teamspace_members(teamspace_id, add?, remove?, role?)` | Add/remove teamspace members | 1–22 | ✅ (removals) |
| `sana_delete_teamspace(teamspace_id)` | Delete an API-managed teamspace | 1 | ✅ |

> `sana_update_user(disabled=True)` deactivates someone reversibly — prefer it over `sana_delete_user`.

### Analytics and reporting

| Tool | Purpose | API calls |
|---|---|---|
| `sana_run_insights_query(sql, output_format?, wait_seconds?)` | Custom read-only SQL against the analytics warehouse | 2–8 |
| `sana_run_learner_progress_report(content_types?, group_ids?, content_ids?, assignment_type?, output_format?, wait_seconds?)` | Built-in progress export | 3–9 |
| `sana_get_report_job(job_id, report_id?)` | Poll a job and collect its download link | 1 |

> Reports are asynchronous. Both run tools poll for up to 20 seconds and then hand back a `jobId` — continue with `sana_get_report_job` rather than re-running the report. Download links expire.

### Escape hatch

| Tool | Purpose | API calls |
|---|---|---|
| `sana_api_request(method, path, query?, body?)` | Call any Sana endpoint directly; returns untrimmed JSON | 1 |

## Example: ground an answer in real content

```
User: "Someone on my team wants to get better at giving feedback — what do we have?"

sana_search_content(query="giving feedback")
  → {results: [{id: "c_9k2", title: "Giving Effective Feedback", contentType: "course",
                description: "Practical frameworks for…", tags: ["management"], score: 12},
               {id: "p_44a", title: "New Manager Path", contentType: "path", score: 5}], count: 2}

sana_get_content(content_type="path", content_id="p_44a")
  → {id: "p_44a", title: "New Manager Path", courseCount: 4,
     courses: [{id: "c_9k2", title: "Giving Effective Feedback", durationMinutes: 45}, …]}

→ Answer names the two real options and what the path contains.
```

## Example: onboard a new hire

```
sana_find_user(query="priya@acme.com")
  → {users: [{id: "u_71", email: "priya@acme.com", name: "Priya Patel"}], count: 1}

sana_add_group_members(group_id="g_new", users=["priya@acme.com"])
  → {groupId: "g_new", added: ["u_71"], role: "learner", count: 1}

sana_assign_content(user="priya@acme.com", content_ids=["c_9k2"], due_date="2026-09-30T00:00:00Z")
  → {userId: "u_71", assigned: ["c_9k2"], count: 1}

sana_get_user_assignments(user="priya@acme.com")
  → {userId: "u_71", assignments: [{contentId: "c_9k2", title: "Giving Effective Feedback",
                                    dueDate: "2026-09-30T00:00:00Z", status: "not_started"}], count: 1}
```

## Development

```bash
uv venv && uv pip install -e ".[dev]"
uv run pytest
uv build
```

The test suite runs entirely offline — no network, no credentials.

See `example_skill/SKILL.md` for a ready-to-use Claude Skill carrying the multi-step recipes.

| Module | Responsibility |
|---|---|
| `server.py` | The MCP surface only: tool declarations, validation, delegation |
| `auth.py` | Environment resolution, base-URL normalization, token cache |
| `client.py` | Authenticated requests, retries, envelopes, pagination |
| `search.py` | Content catalog cache and local ranking |
| `users.py` | Email/id resolution for people |
| `reports.py` | SQL guardrails and bounded job polling |
| `views.py` | Response trimming and output caps |

## Troubleshooting

- **"SANA_DOMAIN is not set"** — the server starts but every tool fails. Set `SANA_DOMAIN`, `SANA_CLIENT_ID`, and `SANA_CLIENT_SECRET` in the MCP client's `env` block. The startup line on stderr shows what was resolved.
- **"Sana rejected the client credentials (HTTP 401 from /api/token)"** — the client ID/secret is wrong, or the API client is disabled in Sana.
- **403 on a write tool** — the token scope is read-only. Set `SANA_SCOPE=read,write` and ensure the API client has write permission.
- **Newly published content missing from search** — the catalog cache is up to 5 minutes old. Call `sana_search_content(refresh=true)`.
- **A report comes back `pending`** — that's normal for large exports. Poll `sana_get_report_job(job_id)`; re-running the report starts the work over.
- **`catalogTruncated: true`** — the catalog exceeds the page bound. Narrow with `content_types` or `tags`.

## License

MIT — see [LICENSE](LICENSE).
