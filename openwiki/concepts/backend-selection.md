---
type: concept
title: Backend Selection (Slides and Summarize)
description: How describe_slides and summarize select their openrouter and agno backends from config, why only those values are valid, and how unknown values fail without fallback.
tags: [transcriber, backends, openrouter, slides, summarize, notion, config]
verified:
  - by: openwiki/0.9.0
    at: 2026-11-07T16:24:10.535Z
sources:
  - id: openwiki-source-df4110b2c5338913ae9eedcf
    resource: repo://transcriber/__main__.py
  - id: openwiki-source-7eaaa16b2c5b3baf4675d042
    resource: repo://transcriber/backends/__init__.py
  - id: openwiki-source-dbc6c72d3aa9191bc8540121
    resource: repo://transcriber/backends/errors.py
  - id: openwiki-source-8a3a341e370fcf0ec5b903de
    resource: repo://transcriber/backends/interfaces.py
  - id: openwiki-source-a3b622f6f01eeb4e4042c7e0
    resource: repo://transcriber/backends/notion_publish.py
  - id: openwiki-source-209e1f0671f9315f4a7eb9c7
    resource: repo://transcriber/backends/openrouter.py
  - id: openwiki-source-d03ac1f85a9e3bd2b413eec1
    resource: repo://transcriber/backends/slides.py
  - id: openwiki-source-8ae57789b707a6fc3e3769d7
    resource: repo://transcriber/backends/summarize_agno.py
  - id: openwiki-source-5670223669d02f5c4fdf5d64
    resource: repo://transcriber/backends/summarize.py
  - id: openwiki-source-c4777b8db8d4806695ac8b6a
    resource: repo://transcriber/config.py
generated: { by: "openwiki/0.9.0", at: "2026-11-07T16:24:10.535Z" }
---

# Backend Selection (Slides and Summarize)

The two post-transcript stages each select a pluggable backend as a **pure function of config**. Selection happens at orchestrator runtime, not inside the media pipeline. Each stage has exactly one valid backend today, and an unexpected value raises instead of silently falling back to another backend.

## Stage/backend matrix

| Stage | Config selector | Valid backends | Default |
|---|---|---|---|
| `describe_slides` | `config.stages.slides.backend` | `"openrouter"` | `"openrouter"` |
| `summarize` | `config.summary.backend` | `"agno"` | `"agno"` |

The two selections are orthogonal: they do not change each other. The supported combination in the shipped code is `slides=openrouter` + `summarize=agno`.

The concrete OpenRouter consumers use distinct per-stage models from one shared `openrouter` block:

* the slides `openrouter` backend uses `config.openrouter.slides_model` (`google/gemini-2.5-flash` by default);
* the summarize `agno` backend uses `config.openrouter.summary_model` (`google/gemini-2.5-pro` by default).

So the shared block is **one block, two per-stage models**. The `openrouter` section itself is unconditionally required because transcription also runs over the OpenRouter HTTP seam, and `notion.token_env` is required when `summary.backend == "agno"` because the engine publishes the Notion subpage itself.

## Validation and no-silent-fallback

Selection is validated twice with the same fail-fast semantics:

* **Config load:** `stages.slides.backend` must be in `SLIDES_BACKENDS` (`("openrouter",)`) and `summary.backend` must be in `SUMMARY_BACKENDS` (`("agno",)`). Anything else raises `ConfigError` during `load`. Omitted backends fall back to the dataclass defaults (`SlidesStage.backend = "openrouter"`, `Summary.backend = "agno"`), so a minimal config still selects the only valid backends.
* **Orchestrator call site:** `_get_slides_backend(config)` returns `OpenRouterSlidesBackend()` only for `"openrouter"` and raises `ValueError` otherwise; `get_summarize_backend(config)` returns `AgnoSummarizeBackend()` only for `"agno"` (imported lazily inside the function) and raises `ValueError` otherwise.

There is **no silent cross-backend fallback** per stage. A backend failure surfaces as its stage error (`SlideDescribeError` / `SummarizeError`) and fails that recording's stage; the batch records the failure and continues with the next recording.

## Why only `openrouter` / `agno`

`openrouter` is the only slides backend and `agno` is the only summarize backend by deliberate scope decision, enforced by the `SLIDES_BACKENDS` / `SUMMARY_BACKENDS` tuples above. The old `agy` CLI backend has been removed: the only remnants are the optional, ignored `Agent.cli` / `Agent.extra_args` fields, kept purely so existing host configs that still carry them continue to load. There is no `AgySummarizeBackend`, no `_uses_agy` helper, and no `agy` branch in either selector in the shipped code.

The name `agno` is retained for config compatibility, but the summarize backend no longer drives an Agno agent. It calls the OpenRouter REST API directly via `_openrouter_chat` and reads `choices[0].message.content` straight from the response. This avoids the truncation previously caused by Agno's `get_content_as_string()` on reasoning models that return a separate `reasoning` channel alongside `content`. The shared seam in `transcriber.backends.openrouter` documents this explicitly: both `_openrouter_chat` and the slides wrapper `_openrouter_vision` read `message.content` directly.

## Visibility: `--dry-run` and `check`

The selected backend per stage is visible before any paid or network call:

* the dry-run plan built by `_enabled_stage_names` / `print_plan` lists `describe-slides [<backend>]` and `summarize+notion [<backend>]` alongside `transcribe [openrouter:<model_id>]`, plus the Notion / Telegram / S3 destinations;
* `transcriber check` (via `_cmd_check` plus `preflight_check`) prints `describe_slides backend='...'` and `summarize backend='...'` on both the pass and fail paths, so a wrong backend value is observable even when preflight itself fails.

## Interfaces

`transcriber.backends.interfaces` defines two small, pure `Protocol` interfaces selected per stage by config:

* `SlidesBackend` — turns extracted slide images into a `<name>.slides.md` markdown block.
* `SummarizeBackend` — turns the transcript (+ the slide markdown) into `<name>.md` + `<name>.telegram.md` and publishes to Notion.

Shared data types:

* `SlideInput` — a single extracted slide plus its scene timing (`image_path`, `timestamp`). `timestamp` is `MM:SS - MM:SS` or the literal `"unknown"`; timing is never omitted.
* `SummaryResult` — paths to the two files a summarize backend writes (`summary_path` = `<name>.md`, `telegram_path` = `<name>.telegram.md`). Both files are written by the backend; the engine reads them back (file-is-source-of-truth contract).

Method contracts:

* `SlidesBackend.describe(slides, transcript_text, config, *, timeout) -> str` — the `openrouter` implementation is image-only: it accepts `transcript_text` to satisfy the protocol and the orchestrator call site but intentionally ignores it. Transcript cross-referencing is the summarize stage's job.
* `SummarizeBackend.summarize(transcript_path, slides_markdown, recording_dir, config) -> SummaryResult` — `slides_markdown` is embedded when non-empty and treated as no slides when `None` / empty / whitespace.

## What each backend does

`OpenRouterSlidesBackend` issues one OpenRouter vision call per slide via `_openrouter_vision`, concatenating the per-slide markdown. An empty slide set returns `""` with no HTTP call; a valid empty response for a slide contributes nothing. Over-ceiling decks (slide count via `openrouter.max_slides`, default `60`, or total encoded bytes) raise `SlideDescribeError` before any send. See the [Slides Description](../workflows/slides-description.md) workflow for the full stage flow.

`AgnoSummarizeBackend` produces the summary and digest with two direct OpenRouter calls (full Markdown summary, then a short Telegram digest derived from that summary), writes `<name>.md` (+ `<name>.slides-clean.md` and `<name>.telegram.md` when non-empty) before publishing, then publishes the Notion subpage engine-side via `publish_to_notion` — no MCP, no model tool-calling. A separate single-call partition (`build_routed_digests`) splits the summary into per-topic Telegram buckets when routing is configured. The stage time bound is `timeouts.summarize` when set, otherwise the default timeout; an empty or absent `<name>.md` fails the stage. See [Summarize and Publish](../workflows/summarize-publish.md) for the artifact and idempotency details.

For the `agno` path Notion is always a direct REST publish (parent page id extraction, Markdown-to-blocks conversion, `<=100`-block chunking, optional `Slide Descriptions` child page). The `notion_mcp` helper module remains in the tree but is not on the summarize run path.

## Shared errors and hygiene

`transcriber.backends.errors` owns the shared backend error types:

* `SlideDescribeError` — transport, non-2xx after retries, malformed JSON, or missing `choices[0].message.content` in the slides backend. A deliberately empty result is **not** an error.
* `SummarizeError` — any failure in the `agno` summarize backend, including timeouts and the non-empty `<name>.md` post-condition.
* `TranscribeError` — failure in the separate OpenRouter STT path (not a slides/summarize backend, but the same hygiene contract).

All three follow the same log-hygiene contract: messages and logs carry only stage, model, sizes, and elapsed time (errors add HTTP status plus a short truncated body snippet), never the API key, request headers, tokens, or base64 image/audio bytes.

## Related

* [Config Model](config-model.md)
* [OpenRouter](../integrations/openrouter.md)
* [Slides Description](../workflows/slides-description.md)
* [Summarize and Publish](../workflows/summarize-publish.md)
