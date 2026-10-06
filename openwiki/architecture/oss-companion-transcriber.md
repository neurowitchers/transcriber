---
type: architecture
title: OSS Companion Transcriber Engine
description: Orchestrator, deterministic media pipeline, pluggable post-transcript backends, Notion/Telegram/S3 publishing, S3 sync, and manifest-based idempotency for the transcriber.
tags: [transcriber, pipeline, backends, notion, telegram, s3, idempotency, orchestrator]
verified:
  - by: openwiki/0.6.1
    at: 2026-10-06T06:48:26.024Z
sources:
  - id: openwiki-source-df4110b2c5338913ae9eedcf
    resource: repo://transcriber/__main__.py
  - id: openwiki-source-8a3a341e370fcf0ec5b903de
    resource: repo://transcriber/backends/interfaces.py
  - id: openwiki-source-209e1f0671f9315f4a7eb9c7
    resource: repo://transcriber/backends/openrouter.py
  - id: openwiki-source-d03ac1f85a9e3bd2b413eec1
    resource: repo://transcriber/backends/slides.py
  - id: openwiki-source-9358a8011a5f64a66c35ac0b
    resource: repo://transcriber/cleanup.py
  - id: openwiki-source-c09b28db65820f5184d0fc9f
    resource: repo://transcriber/pipeline.py
  - id: openwiki-source-885867c6094490b39c8c40d0
    resource: repo://transcriber/publish/s3.py
  - id: openwiki-source-0c6dbb86c6b6001bf65f1b4c
    resource: repo://transcriber/state.py
generated: { by: "openwiki/0.6.1", at: "2026-10-06T06:48:26.024Z" }
---

# OSS Companion Transcriber Engine

The transcriber engine is a local-first Python CLI that turns `.mp4` recordings into a plain-language transcript, an optional slide-description markdown block, a summary/notion page, a Telegram digest, and an optional S3 sync. The engine is composed of three separable concerns:

1. **Configuration** — a validated dataclass model loaded from `.json`/`.yaml`/`.yml`, with secrets referenced by environment-variable *name* only and resolved at use time.
2. **Deterministic media pipeline** — offline child-process stages (ffmpeg audio extract, optional scenedetect slide/scene extraction, OpenRouter STT transcription).
3. **Manifest-gated orchestrator** — the paid/network stages (describe_slides, summarize, Notion, Telegram, S3, cleanup) that run only after the media pipeline and are guarded by a per-recording `<name>.transcriber_state.json` manifest.

The two post-transcript stages (`describe_slides` and `summarize`) each select a pluggable backend from config. `describe_slides` always runs on the `openrouter` vision backend. `summarize` selects `agy` (default) or `agno`. The Notion publish path is **backend-specific**: `agy` publishes via its own Notion MCP as part of its run; `agno` publishes directly via the Notion REST API with no MCP and no model tool-calling.

> **Boundary:** the `pipeline` stage is media-only. Paid/network calls (OpenRouter STT is called inside the pipeline's transcribe stage, but the slides/summarize/Notion/Telegram/S3 steps live in the manifest-gated orchestrator). The docs phrase this as "the pipeline stage stays media-only (ffmpeg / scenedetect / transcribe)". In practice the transcribe stage itself reaches OpenRouter, so the "paid/network" gate is best read as *post-transcript* orchestration being manifest-gated, with the STT HTTP seam living inside `pipeline._transcribe`.

## Entry points


- `transcriber [--config PATH] [--keep-intermediates] [--dry-run]` — load config, pre-flight check, discover new recordings, run the batch, print a batch summary, exit non-zero if any recording failed.
- `transcriber check [--config PATH]` — run the pre-flight check standalone; exit 0 when all required binaries/env vars are present, non-zero otherwise.

Dry-run prints the execution plan (including Notion server/parent page, Telegram chats, and S3 bucket/profile where enabled) and performs no side effects. `--keep-intermediates` is also toggled persistently when `config.debug` is true.

### Pre-flight check (E2)

<!-- openwiki: broken internal link [../transcriber/__main__.py] file "../transcriber/__main__.py" does not exist. Fix the href or restore the target, then delete this comment. -->
[`__main__.py`](../transcriber/__main__.py) implements the pre-flight check as `preflight_check`, which verifies required binaries on `PATH` (via `shutil.which`) and required environment variables (by resolving the env-var *name* through `resolve_env`). OpenRouter is now **mandatory**: a missing `openrouter` config section fails fast because transcription runs over its HTTP seam.

Required binaries depend on enabled stages:

- `ffmpeg` is always required.
- `scenedetect` is required when `config.stages.slides.enabled`.
- `agy` is required when `config.summary.backend == "agy"`.
- `aws` is required when S3 sync is enabled.

Required env vars depend on config:

- The Telegram bot token env (always, for dissemination).
- The OpenRouter API key env (always, now that transcription uses the OpenRouter HTTP seam).
- The Notion token env when `summary.backend == "agno"` (and `config.notion.token_env` is set).

## Configuration model

<!-- openwiki: broken internal link [../transcriber/config.py] file "../transcriber/config.py" does not exist. Fix the href or restore the target, then delete this comment. -->
Configuration is loaded in [`config.py`](../transcriber/config.py) into a single validated dataclass. Both `.json` and `.yaml` map to the same model. Secrets are never stored in the model; the config references them by env-var name only and `resolve_env` resolves them at use time.

Notable model pieces:

- `SlidesStage` — `enabled` + `backend` (only `"openrouter"` is allowed; validated at load and `--dry-run`).
- `Stages` — `slides: SlidesStage` and `s3_sync: bool`.
- `Transcribe` — `model_id`, `diarize`, `segment_seconds`, `overlap_seconds`.
- `Summary` — `language`, `sections`, `backend` (`"agno"`).
- `Agent` — `cli`, `extra_args`, `output_file` (templated with `{basename}`).
- `OpenRouter` — `api_key_env`, `base_url`, `slides_model`, `summary_model`, `max_slides`.
- `Notion` — `server`, `parent_page_id`, `insert`, `token_env`.
- `Telegram` — `bot_token_env`, `default_chat_id`, `routing`, etc.
- `S3` — `bucket`, `profile`.
- `Timeouts` — per-stage timeouts including `ffmpeg`, `scenedetect`, `slides`, `summarize`, `agno`, `s3`, and a top-level `transcribe` timeout.

Config validation enforces:

- `slides.backend` must be `"openrouter"` (selected in `_get_slides_backend` and validated at load).
- `summary.backend` must be `"agno"` (enforced in `get_summarize_backend` with no silent cross-backend fallback).

## Deterministic media pipeline

<!-- openwiki: broken internal link [../transcriber/pipeline.py] file "../transcriber/pipeline.py" does not exist. Fix the href or restore the target, then delete this comment. -->
[`pipeline.py`](../transcriber/pipeline.py) ports the deterministic media stages using the `duct` library. Each child process is wrapped with a configurable timeout drawn from `config.timeouts`. Each stage is idempotent: a step whose output already exists is skipped. The pipeline returns a structured `RecordingResult` listing which artifacts are new.

Stages, in order:

1. **Audio extract** — `ffmpeg` mp4 -> mp3.
2. **Slide/scene extraction** (optional, when `config.stages.slides.enabled`) — `scenedetect`.
3. **Transcribe** — audio is diarized and chunked into explicit time slices, each slice sent to the OpenRouter speech-to-text API, and the slices are stitched back into `<name>.txt` (plain readable transcript, no timestamp markers).

The pipeline's per-recording driver is `process_recording(mp4, config)`, which returns a `RecordingResult(name, mp4, new_artifacts, skipped_artifacts)`. `process_all(config)` runs it over every `.mp4` under `config.recordings_dir` in sorted order.

### Pipeline as the media-only seam

The pipeline stays ffmpeg + scenedetect + transcribe. It does **not** call the slides or summarize backends. Those live in the manifest-gated orchestrator step (`_process_one`). Concretely:

- The slides backend is not called from `pipeline.process_recording`.
- The summarize backend is not called from the pipeline.

The only network seam inside the pipeline is the OpenRouter STT call via `transcriber.backends.transcribe_openrouter.transcribe_recording`, invoked from `pipeline._transcribe`.

### Stage helpers

- `_extract_audio(mp4, mp3, timeout)` — mirrors the source ffmpeg flags.
- `_extract_slides(mp4, slides_dir, scenes_csv, timeout)` — mirrors the source scenedetect command.
- `_transcribe(mp3, txt, config)` — delegates to `transcriber.backends.transcribe_openrouter.transcribe_recording`, which chunks the audio, transcribes each part via the OpenRouter `/audio/transcriptions` seam (diarized when `config.transcribe.diarize`), stitches + reconciles speakers, and writes a speaker-attributed `<name>.txt`.

`_run_command` is the sole seam through which the pipeline invokes external binaries. Tests monkeypatch it to avoid spawning real processes.

## Per-recording state manifest (idempotency)

<!-- openwiki: broken internal link [../transcriber/state.py] file "../transcriber/state.py" does not exist. Fix the href or restore the target, then delete this comment. -->
Idempotency is driven by [`state.py`](../transcriber/state.py), which gives each recording a sidecar JSON manifest `<name>.transcriber_state.json` living next to the `.mp4`. The manifest — not artifact existence alone — is the source of truth for stage completion (requirement E1).

Canonical ordered stages tracked by the manifest:

1. `pipeline`
2. `describe_slides`
3. `summarize`
4. `notion`
5. `telegram`
6. `s3`
7. `cleanup`

`describe_slides` runs (when `stages.slides.enabled`) the configured slides backend to produce `<name>.slides.md` before summarize consumes it.

`notion` is folded into the `summarize` stage in practice (both summarize backends publish to Notion as part of the same run), but it is tracked separately so a future split does not break the manifest schema.

Pre-existing manifests written before `describe_slides` existed simply lack that key. Because completion is read as `_completed.get(stage, False)`, an absent `describe_slides` is treated as *incomplete*, so re-running a recording whose manifest predates this stage re-issues the slides call exactly once (and subsequently records it, making further re-runs idempotent).

`RecordingState` persists after each `mark_complete(stage)` call, so a crash between stages still leaves an accurate manifest for the next retry. On full success the orchestrator calls `state.delete()`.

## Orchestrator and manifest-gated stages

<!-- openwiki: broken internal link [../transcriber/__main__.py] file "../transcriber/__main__.py" does not exist. Fix the href or restore the target, then delete this comment. -->
The orchestrator is `_process_one` in [`__main__.py`](../transcriber/__main__.py). It runs each stage in order, honoring the state manifest, and catches exceptions per-recording so the batch continues. Cleanup runs only on a recording's full success (unless intermediates are kept).

```mermaid
sequenceDiagram
    participant Orchestrator as orchestrator (_process_one)
    participant State as state manifest
    participant Pipeline as pipeline.process_recording
    participant Slides as describe_slides backend
    participant Summarize as summarize backend
    participant Telegram as TelegramPublisher
    participant S3 as s3.sync
    participant Cleanup as cleanup

    Orchestrator->>State: load RecordingState(mp4)
    note over Orchestrator: stage = pipeline
    alt pipeline not complete
        Orchestrator->>Pipeline: process_recording(mp4, config)
        Pipeline-->>Orchestrator: RecordingResult
        Orchestrator->>State: mark_complete(pipeline)
    else already complete
        Orchestrator->>State: is_complete(pipeline) == True
    end

    note over Orchestrator: stage = describe_slides
    alt slides enabled and not complete
        Orchestrator->>Slides: backend.describe(slides, transcript, config, timeout)
        Slides-->>Orchestrator: slides_markdown
        Orchestrator->>State: write <name>.slides.md + mark_complete(describe_slides)
    else slides disabled or already complete or artifact present
        Orchestrator->>State: skip / mark_complete(describe_slides)
    end

    note over Orchestrator: stage = summarize + notion
    alt not complete
        Orchestrator->>Summarize: backend.summarize(transcript, slides_markdown, recording_dir, config)
        note right of Summarize: agy: writes <name>.md + <name>.telegram.md + Notion via MCP
        note right of Summarize: agno: writes <name>.md + <name>.telegram.md; engine publishes Notion via REST
        Summarize-->>Orchestrator: SummaryResult
        Orchestrator->>State: mark_complete(summarize) + mark_complete(notion)
    else already complete
        Orchestrator->>State: skip
    end

    note over Orchestrator: stage = telegram
    alt not complete
        Orchestrator->>Telegram: send(digest or full summary)
        Telegram-->>Orchestrator: responses
        Orchestrator->>State: mark_complete(telegram)
    else already complete
        Orchestrator->>State: skip
    end

    note over Orchestrator: stage = s3
    alt s3 enabled and not complete
        Orchestrator->>S3: sync(recording_dir, config)
        S3-->>Orchestrator: True
        Orchestrator->>State: mark_complete(s3)
    else disabled or already complete
        Orchestrator->>State: skip
    end

    note over Orchestrator: stage = cleanup
    alt not keep_intermediates and not complete
        Orchestrator->>Cleanup: cleanup(mp4, keep_intermediates=False)
        Cleanup-->>Orchestrator: deleted paths
        Orchestrator->>State: mark_complete(cleanup) + state.delete()
    else keep_intermediates or already complete
        Orchestrator->>State: skip
    end
```

### Stage: describe_slides

When `config.stages.slides.enabled` and the manifest is not complete, the orchestrator:

1. Selects the slides backend via `_get_slides_backend(config)` (only `openrouter`).
2. Builds slide inputs via `backends_mod.build_slide_inputs(mp4.parent, name)`.
3. Reads the transcript text from `<name>.txt`.
4. Calls `backend.describe(slides, transcript_text, config, timeout=slides_timeout)`.
5. Writes the result to `<name>.slides.md` and marks `describe_slides` complete.

If `<name>.slides.md` already exists (e.g. from a prior crash after write but before mark), the orchestrator skips the paid call and marks complete. If slides are disabled, it logs and skips.

The slides backend is **image-only**: it never receives the transcript. Transcript cross-referencing is the summarize stage's job. Each slide is sent in its own OpenRouter vision call carrying a single image and no transcript; the per-slide markdown is concatenated.

#### describe_slides is only openrouter

`_get_slides_backend` returns `OpenRouterSlidesBackend()` when `config.stages.slides.backend == "openrouter"`, and raises `ValueError` otherwise. Selection is validated at config load; an unexpected value here raises rather than silently falling back.

### Stage: summarize + notion

When the manifest is not complete for both `summarize` and `notion`, the orchestrator:

1. Reads the prepared slide markdown from `<name>.lides.md` if slides were enabled and the file exists; otherwise `None`.
2. Selects the summarize backend via `backends_mod.get_summarize_backend(config)` (pure function of `config.summary.backend`).
3. Calls `backend.summarize(transcript_path, slides_markdown, recording_dir, config)`.
4. Marks both `summarize` and `notion` complete.

`slides_markdown` is embedded when non-empty and omitted when `None`/empty/whitespace. When slides are disabled, a stale `<name>.lides.md` is never fed into the summary — treated exactly like slides-off.

Both backends produce the same artifacts (`<name>.md` + `<name>.telegram.md`) and publish to Notion, but the publish path is backend-specific.

#### summarize: agno

`AgnoSummarizeBackend` wraps `transcriber.agent.run_agent`, which writes `<name>.md` + `<name>.telegram.md` and publishes to Notion via `agno`'s own MCP, applying the non-empty `<name>.md` post-condition. Behavior is unchanged from the pre-split engine.

#### summarize: agno

`AgnoSummarizeBackend` is imported lazily inside `get_summarize_backend` so the base engine never needs the optional `agno` extra unless selected. It drives an Agno agent with a configurable OpenRouter model to produce the meeting summary and a short digest as plain text. The **engine** then publishes a Notion subpage by calling the Notion REST API directly (`transcriber.backends.notion_publish.publish_to_notion`) — no MCP, no model tool-calling.

Rationale: the official Notion MCP's tool schemas (`oneOf`/`anyOf`/`$ref`) break tool-calling on Gemini/Mistral over OpenRouter (empty `null` completions, zero tool calls), so a deterministic engine-side publish is used instead.

`AgnoSummarizeBackend` hard time-bounds the async run under `asyncio.run` with a timeout equal to the summarize-stage timeout (`timeouts.summarize` when set, else `timeouts.agy`). The blocking Notion publish runs in a worker thread so the timeout can still cancel the run.

After the run, the engine re-uses the same non-empty `<name>.md` success check as the `agno` path: an empty or absent summary file fails the stage (retryable).

Summarize + Notion publish happen in a single run, so a Notion failure fails the whole `summarize`+`notion` stage, like `agno`.

#### Notion publishing: agy vs agno

- `agno` publishes via `agno`'s own Notion MCP as part of its run.
- `agno` publishes directly via the Notion REST API (`transcriber.backends.notion_publish.publish_to_notion`); no MCP, no model tool-calling.

The direct REST path:

1. Extracts the parent **page id** from `notion.parent_page_id` (accepts either a bare id — hyphenated or not — or a full Notion URL with the id embedded).
2. Converts the Markdown summary into Notion **block** objects, **losing nothing**: headings, paragraphs, bullet/numbered list items, blockquotes and fenced code blocks map to their native block types; anything else falls back to a paragraph block so all text survives.
3. Creates a new subpage under the parent (title = the recording basename) with up to the first 100 blocks, then appends the remainder in <=100-block chunks (Notion caps children per request at 100).
4. When cleaned slide descriptions are supplied, creates a child page titled "Slide Descriptions" **under the summary page** holding the slide blocks, so the summary page stays short and the long slide detail lives one click away.

Secrets: the token is resolved by env-var *name* (`notion.token_env`) at use time and is **never logged**.

### Stage: telegram

When the manifest is not complete, the orchestrator sends the digest or full summary via `TelegramPublisher(config).send(message_text)`.

It prefers the concise digest agy wrote for chat; falls back to the full summary only if the digest is missing/empty.

Notion publishing via the Bot HTTP API:

- Resolves the bot token from the environment variable **named** in `config.telegram.bot_token_env` (never a literal secret).
- Routes a message to a chat id based on its topic: matched topics use `config.telegram.routing[topic]`; unmatched or unidentified topics fall back to `config.telegram.default_chat_id`.
- Chunk messages that exceed Telegram's per-message character limit into multiple `sendMessage` calls.

Message language is decided upstream (per `config.summary.language`); the caller supplies the final text and this module only routes and sends it.

<!-- openwiki: broken internal link [url] file "url" does not exist. Fix the href or restore the target, then delete this comment. -->
- Telegrams are sent with no `parse_mode`, so Markdown syntax would otherwise render literally. The module strips Markdown to clean plain text before sending: `**bold**`/`__bold__`/`*italic*`/`_italic_` -> inner text; `` `code` `` -> inner text; leading `#` heading markers removed; `-`/`*`/`+` list bullets -> `•` glyph; `> quote` -> quoted text; `[text](url)` -> `text (url)`; `![alt](url)` -> `alt (url)`; fenced-code fences removed.

### Stage: s3

When S3 is enabled and the manifest is not complete, the orchestrator calls `s3_mod.sync(mp4.parent, config)` and marks `s3` complete.

S3 sync is a gated stage: it is a no-op (returns `False`) when `config.stages.s3_sync` is falsy or when `config.s3` is `None`. When enabled it executes `aws s3 sync <local_dir> <config.s3.bucket> --profile <config.s3.profile>` through `duct`, enforcing `config.timeouts.s3` seconds.

Ordering contract: a successful `sync` MUST complete before any cleanup/deletion of intermediates. This module only performs the upload; `transcriber.cleanup` is a separate function the orchestrator calls *after* `sync` returns `True` (or after it is skipped on a run where S3 is disabled).

### Stage: cleanup

Cleanup runs only on success (unless intermediates are kept). It is a separate function the orchestrator calls after a recording's publish (and, when S3 is enabled, a successful `sync`) has completed. This module performs no upload itself; sequencing sync-before-delete is the orchestrator's responsibility.

What's kept:

- source `<name>.mp4`
- final `<name>.md` summary
- `<name>.txt` speech-to-text transcript
- `<name>.slides-clean.md` reader-facing slide descriptions (markers stripped — the durable slides output)

What's deleted:

- `<name>.mp3`
- `<name>.telegram.md`
- `<name>.slides.md` (the markers-kept slide-description **debug** intermediate; its cleaned counterpart `<name>.slides-clean.md` is kept)
- `<name>.notion_published.json` (the `agno` publish record)
- `extracted_slides.<name>/` directory (which also holds `<name>.scenes.csv`)
- `transcribe_work.<name>/` directory (which holds the OpenRouter STT chunk parts `part_*.mp3` and the `segments.json` resume manifest)

The `keep_intermediates` flag (surfaced by the CLI as `--keep-intermediates`) disables all deletion.

## Backends

<!-- openwiki: broken internal link [../transcriber/backends/__init__.py] file "../transcriber/backends/__init__.py" does not exist. Fix the href or restore the target, then delete this comment. -->
[`__init__.py`](../transcriber/backends/__init__.py) surfaces the public backend API:

- Interfaces: `SlidesBackend`, `SummarizeBackend`, and the `SlideInput` / `SummaryResult` data types.
- Errors: `SlideDescribeError`, `SummarizeError`, `TranscribeError`.
- The shared OpenRouter vision helper `_openrouter_vision` (used by the slides `openrouter` backend).
- Concrete backends: `OpenRouterSlidesBackend`, `build_slide_inputs`, `get_summarize_backend`.

<!-- openwiki: broken internal link [../transcriber/backends/interfaces.py] file "../transcriber/backends/interfaces.py" does not exist. Fix the href or restore the target, then delete this comment. -->
[`interfaces.py`](../transcriber/backends/interfaces.py) defines two small, pure `Protocol` interfaces selected per-stage by config:

- `SlidesBackend` — turns extracted slide images (+ transcript context) into a `<name>.slides.md` markdown block.
- `SummarizeBackend` — turns the transcript (+ the slide markdown) into `<name>.md` + `<name>.telegram.md` and publishes to Notion.

### SlideInput

`SlideInput` is a single extracted slide plus its scene timing:

- `image_path`: path to the slide JPEG.
- `timestamp`: human-readable `MM:SS - MM:SS` scene range, or the string `"unknown"` when the timing could not be resolved from the scenes CSV. Timing is never omitted.

### SummaryResult

`SummaryResult` is paths to the two files a summarize backend writes:

- `summary_path`: `<name>.md`
- `telegram_path`: `<name>.telegram.md`

Both files are written by the backend; the engine reads them back (file-is-source-of-truth contract).

### SlidesBackend.describe

`describe(slides, transcript_text, config, *, timeout)` returns the slide-description markdown.

An empty slide set or a valid empty backend response both resolve to `""`; a transport/format failure raises `SlideDescribeError`.

### SummarizeBackend.summarize

`summarize(transcript_path, slides_markdown, recording_dir, config)` writes both files and publishes the Notion subpage.

`slides_markdown` is embedded when non-empty and omitted when `None`/empty/whitespace. The Notion publish path is backend-specific: the `agy` backend publishes via MCP; the `agno` backend publishes directly via the Notion REST API.

## Slides backend (openrouter only)

<!-- openwiki: broken internal link [../transcriber/backends/slides.py] file "../transcriber/backends/slides.py" does not exist. Fix the href or restore the target, then delete this comment. -->
[`slides.py`](../transcriber/backends/slides.py) implements the `describe_slides` backend. `openrouter` is the only slides backend.

`OpenRouterSlidesBackend` issues **one OpenRouter vision call per slide** (via the `_openrouter_vision` helper), turning each extracted slide JPEG into part of the `<name>.lides.md` markdown block. The descriptor is **image-only**: it never receives the transcript.

`build_slide_inputs` lists the extracted slide JPEGs in a deterministic order and joins each to its scene timing parsed from the scenes CSV that scenedetect writes **into the slides dir** (`extracted_slides.<name>/<name>.scenes.csv`), never a `.mp4` sibling.

Design invariants:

- **Empty semantics:** an empty slide set -> `""` with **no** HTTP call; a valid empty/whitespace backend response -> `""` (not an error). A transport/format failure raises `SlideDescribeError`.
- **One call per slide:** each slide is sent in its own vision call carrying a single image and no transcript; the per-slide markdown is concatenated.
- **Per-slide timestamps:** each slide's message carries a `Timestamp: MM:SS - MM:SS` text part placed **immediately before** its `image_url` part. Unknown timing is passed as the literal `unknown` — never omitted.
- **Downscale:** each JPEG is downscaled to a bounded long edge before base64 to control size/cost. This is a size control, **not** redaction.
- **Deterministic ceiling:** module constants cap the max slide count and the max total encoded bytes; exceeding either raises `SlideDescribeError` **before** any HTTP call. Over-ceiling decks hard-fail (no chunking).
- **Log hygiene:** logs carry stage/model/slide-count/elapsed only — never the API key, headers, or image bytes.
- **No pipeline coupling:** the backend is not called from `pipeline.process_recording` — the pipeline stays ffmpeg + scenedetect + transcribe only. It runs in the manifest-gated orchestrator step.

The slides backend no longer *drops* empty slides — it keeps the model's output (markers included) in `<name>.lides.md` so empty scenes stay identifiable. Stripping markers + stray placeholders happens later, in `transcriber.agent.append_slide_descriptions`, only for the summary / Notion output.

## Summarize backends (agnolo / agno)

<!-- openwiki: broken internal link [../transcriber/backends/summarize.py] file "../transcriber/backends/summarize.py" does not exist. Fix the href or restore the target, then delete this comment. -->
[`summarize.py`](../transcriber/backends/summarize.py) implements two backends:

- `AgnoSummarizeBackend` — drives the local `agno` CLI agent exactly as today (`transcriber.agent.run_agent`): writes `<name>.md` + `<name>.telegram.md` and publishes to Notion via `agno`'s own MCP. Behavior is unchanged from the pre-split engine.
- `AgnoSummarizeBackend` — lives in `transcriber.backends.summarize_agno` and is imported **only when selected** (so the optional `agno` extra is not a base dependency). It produces the summary + digest with an Agno/OpenRouter model, then the **engine** publishes the Notion subpage directly via the Notion REST API (`transcriber.backends.notion_publish.publish_to_notion`) — no MCP, no model tool-calling. Use `get_summarize_backend` to obtain the configured backend without importing `agno` on the default path.

Both backends produce the same artifacts, but the Notion publish path is **backend-specific**: `agno` publishes via its own MCP, while `agno` publishes engine-side via the Notion REST API. Selection is a pure function of `config.summary.backend`; there is **no silent cross-backend fallback**.

`get_summarize_backend` returns `AgnoSummarizeBackend()` for `"agno"` (without importing `agno`), and lazily imports `AgnoSummarizeBackend` for `"agno"`. An unknown value raises.

## Notion MCP (agno path)

<!-- openwiki: broken internal link [../transcriber/backends/notion_mcp.py] file "../transcriber/backends/notion_mcp.py" does not exist. Fix the href or restore the target, then delete this comment. -->
[`notion_mcp.py`](../transcriber/backends/notion_mcp.py) launches the **official** Notion MCP server for the Agno summarize backend. This module is engine-owned and self-contained: it configures the Notion MCP integration purely from `notion.token_env` (+ the existing `notion.parent_page_id` / `notion.insert`). It does **not** read or reuse `agno`'s MCP configuration — the `agno` summarize path is independent.

Design notes:

- **Official server.** Launches `@notionhq/notion-mcp-server` over its default STDIO transport. It reads the Notion integration token from the `NOTION_TOKEN` environment variable.
- **Windows-launchable command.** On Windows a bare `npx` frequently fails to spawn from a subprocess (there is no `npx` executable on `PATH` — only `npx.cmd`). The command is built with `npx.cmd` on Windows and `npx` elsewhere.
- **Env is MERGED, never replaced.** The MCP subprocess inherits the parent environment (so `PATH` / `APPDATA` / `SystemRoot` survive on Windows) and only *adds* the resolved Notion token under `NOTION_TOKEN`. It never hands the child a bare `{NOTION_TOKEN: ...}` dict.
- **Secrets by name only.** The token is resolved at *use* time via `resolve_env` from the env-var *name* in `config.notion.token_env`. The token value is never logged.
- **Lifecycle.** `notion_mcp_tools` only *constructs* the `MCPTools` instance. The caller (Task 4's Agno run wrapper) is responsible for opening and closing it — `MCPTools` must be closed on every path (success/error/timeout) so no MCP subprocess is orphaned.

`agno` is imported **lazily** inside this module so the engine only requires the `agno` extra when `summary.backend == "agno"`. A missing import raises a clear, actionable "install the agno extra" error.

The engine only hands the model the publish toolset (not all ~25 tools) to avoid overwhelming tool selection over OpenRouter:

- `API-post-page` — create the new subpage under the parent
- `API-patch-block-children` — write body blocks / append the parent link
- `API-update-page-markdown` — alt: write the page body as markdown
- `API-post-search` — locate the parent if needed
- `API-retrieve-a-page` — confirm/inspect a page

## OpenRouter STT (transcribe stage)

<!-- openwiki: broken internal link [../transcriber/backends/transcribe_openrouter.py] file "../transcriber/backends/transcribe_openrouter.py" does not exist. Fix the href or restore the target, then delete this comment. -->
[`transcribe_openrouter.py`](../transcriber/backends/transcribe_openrouter.py) implements the OpenRouter STT backend used by the pipeline's transcribe stage. Audio is diarized and chunked into explicit time slices, each slice sent to the OpenRouter speech-to-text API, and the slices are stitched back into `<name>.txt` (plain readable transcript, no timestamp markers).

`transcribe_recording(mp3, txt, config)` chunks the audio, transcribes each part via the OpenRouter `/audio/transcriptions` seam (diarized when `config.transcribe.diarize`), stitches + reconciles speakers, and writes a speaker-attributed `<name>.txt` (`Speaker <ID>: <text>` lines with no timestamp markers; flat text when there are no labels).

Per-segment idempotency and a parameter guard live in that stage's `segments.json` manifest.

`_openrouter_transcribe` is the HTTP seam:

- POSTs to `{base_url}/audio/transcriptions` with the caller-provided audio and model.
- Raw base64, **not** a `data:` URI.
- Retries `429` and `5xx` with bounded exponential backoff (a small, capped number of attempts), then raises `TranscribeError`.
- On a `400` or any other non-2xx status after retries, fails immediately — no retry, no silent fallback.
- On malformed JSON, raises `TranscribeError`.
- Log hygiene: never logs the request (it carries the headers + base64 payload); only the exception type name or a truncated body snippet.

## Backend error types

<!-- openwiki: broken internal link [../transcriber/backends/errors.py] file "../transcriber/backends/errors.py" does not exist. Fix the href or restore the target, then delete this comment. -->
[`errors.py`](../transcriber/backends/errors.py) defines the shared error types:

- `SlideDescribeError` — raised when the `describe_slides` stage's backend fails (transport failures, non-2xx responses after retries, malformed JSON, and missing `choices[0].message.content`). A deliberately empty backend result is *not* an error.
- `SummarizeError` — raised when the `summarize` stage's backend fails.
- `TranscribeError` — raised when the `transcribe` stage's OpenRouter STT backend fails (transport failures, non-2xx responses after retries, e.g. a `400` from a model that cannot diarize — no silent fallback, and malformed JSON).

**Log hygiene:** error messages carry only a short, non-sensitive context (HTTP status + a truncated body snippet). They must never embed the API key, request headers, or base64 image/audio bytes.

## Failure isolation and batch semantics

<!-- openwiki: broken internal link [/transcriber/__main__.py] link "/transcriber/__main__.py" is root-absolute, which no real consumer resolves against the repository root (not a coding agent reading the page, not GitHub's Markdown renderer, not a local viewer); use a path relative to this file instead. Fix the href or restore the target, then delete this comment. -->
The batch runs in [`/transcriber/__main__.py`](/transcriber/__main__.py) via `run_batch(config, recordings, keep_intermediates=False)`, which processes each recording with failure isolation and returns per-recording `RecordingOutcome`s.

`_process_one` wraps each stage in a try/except; a raised exception in one recording is caught, logged, recorded in that recording's manifest (as a failed outcome), and the batch continues. Cleanup runs only on a recording's full success.

`main` returns:

- 0 when all recordings succeeded.
- 1 when any recording failed (the batch still ran to completion).
- 2 when config loading failed.

## Related concepts

- [Backend Selection](../concepts/backend-selection.md)
- [Config Model](../concepts/config-model.md)
- [Pre-flight and Idempotency](../operations/pre-flight-and-idempotency.md)
- [Slides Description](../workflows/slides-description.md)
- [Summarize and Publish](../workflows/summarize-publish.md)
- [Transcription Pipeline](../workflows/transcription-pipeline.md)
