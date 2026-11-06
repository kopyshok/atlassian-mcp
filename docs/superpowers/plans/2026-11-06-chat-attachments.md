# Chat attachments implementation plan

> For agentic workers: use superpowers:executing-plans. Approved scope: this chat,
> 2026-11-06 — transfer uploaded chat images through MCP to Jira and Confluence.

**Goal:** Attach an image by its Open WebUI file ID without exposing session tokens
or passing Base64 through the model.

**Architecture:** Keep the incoming Open WebUI session in ASGI request state only.
Add one upload tool to each existing service namespace. Fetch the file from the
configured Open WebUI origin using that session; use the existing per-user
Atlassian clients for uploading. Return confirmed attachment metadata and embedding
markup; existing edit tools update descriptions/pages separately.

**Tech Stack:** Python 3.12, pinned mcp-atlassian 0.23.1, FastMCP 3.4.9, httpx,
stdlib unittest. No new runtime dependencies.

**Spec:** Agreed proposal in [session log](../../../логи/2026-11-06.md), plus user's approval
in this chat including Confluence. This file records concrete implementation choices.

## Global constraints

- No production access, deployment or publication in this task.
- Preserve personal tokens, write guards, tool filtering and existing tools.
- Only UUID file IDs; no input URLs or filesystem paths; no download redirects.
- PNG/JPEG/GIF/WebP, at most 10 MiB per image, bounded download with a 30-second
  deadline. Uploads use the existing Atlassian client's configured timeout.
- Deterministic attachment name chat-<full file UUID>.<detected extension> prevents
  collisions between different files originally named image.png.
- No automatic retry of an upload whose success is unknown; instruct caller to
  inspect attachments first. Repeated Jira calls may create duplicates; Confluence
  Server/DC rejects duplicate names. No promised exactly-once delivery.

## Review focus

- Concurrent and reused MCP sessions must not mix Open WebUI/Jira/Confluence identities.
- Read-only and excluded tools must reject calls without downloading or uploading.
- Redirects, inaccessible files, oversize streams and HTML error pages cannot upload.
- Failure after upload must not claim that description/page was updated.
- Actual FastMCP tool schemas and multipart transfers must be tested, not only mocks.

## Task 1: Transfer and registration

Files: server.py, new chat_attachments.py, new test_chat_attachments.py, Dockerfile.

- [x] Write and run failing unittest cases for authorized file download, UUID
  validation, size/type/error handling, request isolation and upload results.
- [x] Implement read_openwebui_image(file_id: UUID) -> tuple[bytes, str] and
  upload_jira_chat_attachment(ctx, issue_key, file_id),
  upload_confluence_chat_attachment(ctx, content_id, file_id).
- [x] Register upload_chat_attachment in each namespace before exclusions resolve.
- [x] Preserve original authorization in request state, never forwarded as a header.
- [x] Run python -m unittest test_chat_attachments -v; expected all PASS.

## Task 2: End-to-end regression and delivery

Files: test_e2e.py, new test_chat_e2e.py, README.md, CHANGELOG.md, docker-compose.yml,
project context/decision/log documents.

- [x] Write integration tests through the actual HTTP MCP transport with fake
  Open WebUI/Jira/Confluence: exact file bytes, personal credentials, returned
  names/markup, two users, forbidden file, missing token, read-only, exclusions.
- [x] Verify existing Jira update preserves the returned image markup. Check
  Confluence storage representation used by the existing page update tool.
- [x] Run python test_e2e.py and python test_chat_e2e.py; expected exit 0.
- [x] Document limits, upgrade procedure and live acceptance steps; update version.
- [x] Review final diff independently and fix important findings with regression tests.
- [x] Record actual checks and limitations. Commit local work, no push/deployment.

## Execution evidence

- Baseline python test_e2e.py passed with the pinned dependency set on Python 3.12.
- Docker Desktop started locally; image 1.1.0 built and all suites passed inside the network-isolated container.
- Work in the clean current checkout; no isolated worktree or unrelated changes.

- Final review: independent reviewer found automatic POST retries in the pinned
  Atlassian client. Regression reproduced two uploads after 429 + Retry-After;
  disabled client and adapter retries without replacing SSRF adapters. Regression
  now passes with retries explicitly enabled in the environment.
- Ruling: use the public Confluence client POST instead of its bytes-upload helper
  because the latter omits a timeout. Preserve the existing client's networking
  configuration. Duplicate filenames are rejected rather than silently versioned.
- Schema correction: existing Confluence tool takes content_format="storage",
  not is_markdown. Verified actual update through MCP preserves storage markup.
- Final packaged-image verification: original suite PASS; 12 unit tests PASS;
  chat E2E normal/read-only/excluded PASS, including reused connection identities.
- Production access/rendering and deployment remain outside this local change.
