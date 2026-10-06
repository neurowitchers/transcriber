---
type: integration
title: Notion integration
description: Two Notion publish paths in the transcriber — MCP-based publishing for the agy summarize backend and direct REST publishing for the agno backend — plus config fields, token_env handling, and insertion mode.
tags: [transcriber, notion, agy, agno, mcp, rest-api, publish]
sources:
  - id: openwiki-source-63ccc8cb6fa875fca5499670
    resource: repo://tests/test_notion_mcp.py
  - id: openwiki-source-204cc42a4d31dd02dcb479d9
    resource: repo://tests/test_notion_publish.py
  - id: openwiki-source-df4110b2c5338913ae9eedcf
    resource: repo://transcriber/__main__.py
  - id: openwiki-source-185e808417b44f0a004d75da
    resource: repo://transcriber/backends/notion_mcp.py
  - id: openwiki-source-a3b622f6f01eeb4e4042c7e0
    resource: repo://transcriber/backends/notion_publish.py
  - id: openwiki-source-8ae57789b707a6fc3e3769d7
    resource: repo://transcriber/backends/summarize_agno.py
  - id: openwiki-source-5670223669d02f5c4fdf5d64
    resource: repo://transcriber/backends/summarize.py
  - id: openwiki-source-c4777b8db8d4806695ac8b6a
    resource: repo://transcriber/config.py
generated: { by: "openwiki/0.6.1", at: "2026-10-06T06:48:26.024Z" }
verified:
  - by: openwiki/0.6.1
    at: 2026-10-06T06:48:26.024Z
---

# Notion integration

The transcriber publishes every meeting summary to a Notion subpage. There are **two independent publish paths**, selected by `config.summary.backend`:

- **`agy`** — the `agy` CLI agent publishes to Notion through its own Notion MCP as part of the agent run. The engine does **not** own or implement this publish path.
- **`agno`** — the only current summarize backend. The engine publishes the subpage directly via the Notion REST API (`transcriber.backends.notion_publish.publish_to_notion`), with no MCP server and no model tool-calling; it also launches the official Notion MCP server for the agno path's model-driven interactions via `transcriber.backends.notion_mcp`.

Both paths are required for summarize: a summarize run must publish to Notion or fail the stage. The concrete mechanism is backend-specific. Since the summarize backend set is `("agno",)` only, both the direct REST publish and the official Notion MCP server are used for the single summarize backend — the REST publish deterministically creates the page, while the MCP server supports model-driven Notion interactions in the agno agent run.

## Why two paths

The project intent is that summarize always publishes to Notion. In the shipped implementation the `agno` path diverged from the original Notion-MCP plan because the official Notion MCP server's tool schemas (`oneOf`/`anyOf`/`$ref`) break tool-calling on Gemini/Mistral over OpenRouter — the model returns an empty `null` completion with zero tool calls. The engine therefore replaced the model-driven MCP path for `agno` with a deterministic engine-side REST publish.

The `agy` path was left unchanged: `agy`'s Notion work is part of the `agy` run itself, and the engine does **not** hand-roll a bespoke Notion REST client for the `agy` summarize path. That historical description no longer matches the shipped backend model: the engine's summarize backend set is `("agno",)` only (see `transcriber.config.SUMMARY_BACKENDS`), so there is no current `agy` summarize backend and therefore no engine-owned or engine-avoided `agy` summarize + Notion path. The `agy` references that remain are the removed legacy CLI backend — preserved only as ignored config fields such as `agent.cli` and `agent.extra_args` so existing host configs still load.

## Config fields

Notion configuration lives in the `notion` block of the config, modeled by `transcriber.config.Notion`:

| Field | Type | Purpose |
|---|---|---|
| `server` | `str` | Notion server identifier (conceptual; used for logging/dry-run display). |
| `parent_page_id` | `str` | The parent under which the summary subpage is created. Accepts a bare 32-hex id, an already-hyphenated UUID, or a full Notion URL containing the id. |
| `insert` | `str` | Insertion mode (e.g. `"subpage"`). |
| `token_env` | `Optional[str]` | **Env-var name only** for the Notion API token. The actual secret value is never stored in the config model; it is resolved at use time via `transcriber.config.resolve_env`. Required when `summary.backend == "agno"`. |

Because `token_env` is an env-var **name** (not a value), a config loading cleanly can still have `token_env` present without any secret material in the file. For example, the full example config sets `notion.token_env == "NOTION_API_KEY"` while the simple example leaves it `None` because it uses the `agy` backend.

Config validation enforces the dependency: `summary.backend == "agno"` without `notion.token_env` raises `ConfigError` at load time. Pre-flight (`transcriber check`) additionally verifies the referenced env var is actually set before any work starts.

## The agy path — Notion MCP

For the `agy` summarize backend, Notion is published by `agy` itself through its MCP inside the agent run. The engine does not own that publish path, and the `transcriber.backends.notion_mcp` module is **not** used by the `agy` path.

Module `transcriber.backends.notion_mcp` exists for the **agno** path only and is deliberately self-contained: it configures the Notion MCP integration purely from `notion.token_env` (plus the existing `notion.parent_page_id`/`notion.insert`), and it does **not** read or reuse `agy`'s MCP configuration. The “agy” references here mean the removed legacy CLI backend code paths (kept as ignored config fields such as `agent.cli`/`agent.extra_args`), not a current summarize backend the engine could reuse.

## The agno path — direct REST publish

For `summary.backend == "agno"`, the engine publishes directly via the Notion REST API. The implementation lives in `transcriber.backends.notion_publish` and is invoked by `transcriber.backends.summarize_agno.AgnoSummarizeBackend`.

### Entry point

`publish_to_notion(config, title, summary_markdown, *, slides_markdown=None, timeout=DEFAULT_NOTION_TIMEOUT)` creates a new Notion subpage under `notion.parent_page_id` titled with the recording basename (`title`), populated with the summary blocks. It returns the **summary page URL**.

When `slides_markdown` is a non-empty block of cleaned slide descriptions, a child page titled **"Slide Descriptions"** is created **under the summary page** with the slide blocks — so the summary page stays readable and the (often long) slide detail lives one click away. `None`/empty/whitespace → no slides child page is created.

The function raises `SummarizeError` on any failure so the summarize stage fails (retryable). If the summary page is created but the slides child page then fails, the whole call raises; the caller must not record a successful publish, so a retry re-creates both.

### What it does

1. **Extract the parent page id** from `notion.parent_page_id`. `extract_page_id` accepts a bare 32-hex id, an already-hyphenated UUID, or a full Notion URL with the id embedded, and returns a hyphenated lower-case page id. Empty input or input with no extractable id raises `SummarizeError`.

2. **Convert Markdown → Notion blocks**, losing nothing. `markdown_to_blocks` is line-oriented (matching the shape of the produced summaries):
   - `#`/`##`/`###` → `heading_1`/`heading_2`/`heading_3` (Notion has no heading_4+, so deeper headings clamp to `heading_3`).
   - `-`/`*`/`+` → `bulleted_list_item`.
   - `1.`/etc. → `numbered_list_item`.
   - `>` → `quote`.
   - `` ``` `` fenced blocks → a single `code` block (content verbatim, language `plain text`).
   - Blank lines → skipped (Notion spaces blocks itself).
   - Anything else → `paragraph` (nothing is dropped).

   Inline markers (`**bold**` etc.) are kept as literal text so no characters are lost; the converter intentionally does not strip them. Long text is chunked into multiple rich-text objects to respect Notion's 2000-char-per-text-object cap.

3. **Create the subpage** under the parent with up to the first 100 blocks, then append the remainder in `<=100`-block chunks (`_MAX_CHILDREN_PER_REQUEST = 100`). Notion caps children per request at 100.

4. **Optional slides child page** under the just-created summary page, titled "Slide Descriptions", holding the slide blocks.

### Secrets and log hygiene

The Notion token is resolved by env-var **name** (`notion.token_env`) at use time via `resolve_env` and is **never logged**. Error messages carry only a short, non-sensitive context (HTTP status + truncated body snippet), never the token. The `NOTION_VERSION` pinned is `"2022-06-28"`.

### Timeouts

`DEFAULT_NOTION_TIMEOUT = 60.0` is the bounded per-request socket timeout. Without it, `urlopen` blocks indefinitely; because the publish runs inside `asyncio.to_thread`, cancelling the outer `wait_for` cannot stop a stuck urllib worker, so `timeouts.summarize` would not be a hard bound for the Notion phase. A bounded socket timeout makes each request fail fast instead. The agno backend caps the per-request timeout by the summarize-stage budget via `_notion_request_timeout` (`min(summarize_timeout, DEFAULT_NOTION_TIMEOUT)`).

### Idempotency / duplicate-page guard

The agno backend persists the local artifacts (`<name>.md`, `<name>.telegram.md`, optional `<name>.slides-clean.md`) **before** publishing to Notion. It then writes a sidecar `<name>.notion_published.json` (containing `{"url": page_url}`) after a successful publish. If that sidecar already exists (crash between publish and manifest mark), the publish is skipped to avoid creating a duplicate subpage.

## Notion MCP module (agno path only)

The `transcriber.backends.notion_mcp` module launches the **official** Notion MCP server (`@notionhq/notion-mcp-server`) over its default STDIO transport for the `agno` path's model-driven interactions. It is engine-owned and self-contained.

### Launch constants

- `NOTION_MCP_PACKAGE = "@notionhq/notion-mcp-server"` — the npm package for the official Notion MCP server.
- `NOTION_MCP_TOKEN_ENV = "NOTION_TOKEN"` — the environment-variable name the official Notion MCP server reads its token from (the recommended form, not the advanced `OPENAPI_MCP_HEADERS`).
- `NOTION_MCP_COMMAND` — the Windows-launchable STDIO command string. On Windows a bare `npx` frequently fails to spawn from a subprocess (only `npx.cmd` is on PATH), so the command uses `npx.cmd` there and `npx` elsewhere. `-y` auto-confirms the one-time `npx` install prompt.

### Toolset restriction

The official Notion MCP server exposes ~25 tools with large OpenAPI-derived JSON schemas. Handing all of them to the model over OpenRouter overflows/derails tool selection (the model returns an empty/`null` completion with zero tool calls). The publish step therefore restricts the toolset to a handful via `NOTION_MCP_PUBLISH_TOOLS`:

- `API-post-page` — create the new subpage under the parent
- `API-patch-block-children` — write body blocks / append the parent link
- `API-update-page-markdown` — alt: write the page body as markdown
- `API-post-search` — locate the parent if needed
- `API-retrieve-a-page` — confirm/inspect a page

### Env merging

`notion_mcp_env(config)` builds the MCP subprocess environment by **merging** the parent environment (`os.environ`) with the resolved Notion token under `NOTION_TOKEN`. This preserves `PATH`/`APPDATA`/`SystemRoot` (required to spawn `npx` on Windows) while injecting the token. It is never a bare replace. The token is resolved by env-var **name** from `config.notion.token_env` and is never logged. Raises `ValueError` if `notion.token_env` is unset.

### Lazy agno import

`agno` is imported **lazily** inside `notion_mcp` so the engine only requires the `agno` extra when `summary.backend == "agno"`. A missing import raises a clear, actionable `AgnoImportError` with an install hint (`uv sync --extra agno` or `pip install 'transcriber[agno]'`).

### Lifecycle

`notion_mcp_tools(config)` constructs an `MCPTools` instance bound to the official Notion MCP server with the restricted toolset and the merged env. The returned instance is **not** connected — the caller opens it (via the async context manager or `connect()`) and is responsible for closing it on every path (success/error/timeout) so no MCP subprocess is orphaned.

## Orchestration and pre-flight

The orchestrator in `transcriber.__main__` treats backend selection as a pure function of config. For summarize, `get_summarize_backend(config)` returns `AgnoSummarizeBackend()` for `"agno"` by lazily importing that class; there is no `agy` summarize branch, no import-free `AgySummarizeBackend()` path, and no silent cross-backend fallback — an unknown value raises.

Pre-flight (`transcriber check` / `preflight_check`) verifies the env vars referenced by name are actually set — including `notion.token_env` when `summary.backend == "agno"`. It does not gate the check on the env var being *truthy*; it resolves it by name via `resolve_env`, and a missing/empty referenced env var is reported as a pre-flight problem. Secret values are never read by the pre-flight check itself; it only checks presence by name.

The dry-run plan surfaces the selected backend per stage, including the per-stage OpenRouter model where relevant (e.g. `summarize+notion [<backend>]`).

## Artifact contract and idempotency

Regardless of backend, summarize + Notion publish happen in a single run. Today that backend is `agno`: the engine writes the local artifacts first, then publishes directly via the Notion REST API. A Notion failure fails the whole `summarize`+`notion` stage. Already-completed stages are skipped on retry via the per-recording state manifest (no duplicate Notion pages / Telegram sends / re-sync).

## Focused tests

- `tests/test_notion_mcp.py` — covers the official package/token-env constants, the Windows-launchable command form, env-merge (not replace), token resolution by name, that the token is never logged, the factory launching MCP with the restricted publish toolset, that `MCPTools` is closed on the exception path (no orphaned subprocess), the actionable `AgnoImportError` on missing `agno`, and that the module contains no reference to `agy`'s MCP config.
- `tests/test_notion_publish.py` — covers `extract_page_id` (hyphenated UUID, bare hex, Notion URL, uppercase lowered, empty/no-id raises), `markdown_to_blocks` (heading mapping incl. deeper-heading clamping to `heading_3`, bullets/numbers, quote/code/paragraph, blank-line skipping, inline markers kept verbatim, long-paragraph chunking), bounded per-request timeout passed to `urlopen`, socket `TimeoutError` wrapped as `SummarizeError`, the default bounded timeout, and the slides child subpage creation (parented under the summary page, titled "Slide Descriptions", absent when slides are empty/whitespace).

## Related

- [Backend selection and interfaces](../concepts/backend-selection.md) — how `agno` is selected and why Notion MCP is still required for summarize in spirit.
- [Config model](../concepts/config-model.md) — config fields, secret-by-name resolution, and validation.
- [Pre-flight and idempotency](../operations/pre-flight-and-idempotency.md) — env-var and binary checks before any work starts.
- [Summarize + publish workflow](../workflows/summarize-publish.md) — the end-to-end summarize stage including Notion publish.
