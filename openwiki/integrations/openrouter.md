---
type: integration
title: OpenRouter Integration
description: Shared OpenRouter HTTP seam, OpenAI-format transcription and vision calls, per-stage model selection, env-var-based API key handling, and retry/timeout behavior across the transcriber's paid stages.
tags: [openrouter, api, backend, auth, http, transcriber, slides, transcription]
verified:
  - by: openwiki/0.6.1
    at: 2026-10-02T19:10:14.922Z
sources:
  - id: openwiki-source-df4110b2c5338913ae9eedcf
    resource: repo://transcriber/__main__.py
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
  - id: openwiki-source-005cc63fa54282b91136f803
    resource: repo://transcriber/backends/transcribe_openrouter.py
  - id: openwiki-source-c4777b8db8d4806695ac8b6a
    resource: repo://transcriber/config.py
generated: { by: "openwiki/0.6.1", at: "2026-10-02T19:10:14.922Z" }
---

# OpenRouter Integration

The transcriber depends on OpenRouter _unconditionally_: it is the speech-to-text (STT) backend for the `transcribe` stage, and it also backs the `describe_slides` stage (OpenRouter vision) and the `agno` summarize path (via Agno's OpenRouter model). There is no second paid API provider. This page documents the shared configuration block, the two HTTP seams, per-stage model selection, API-key environment handling, and the retry/timeout/partial-failure behavior that surrounds them.

> Related pages: [Backend Selection](../concepts/backend-selection.md), [Config Model](../concepts/config-model.md), [Transcription Pipeline](../workflows/transcription-pipeline.md), [Slides Description](../workflows/slides-description.md), [OSS Companion Transcriber](../architecture/oss-companion-transcriber.md).

## Scope

OpenRouter shows up in three places in the codebase, but two HTTP seams dominate the integration story:

- **`transcriber.backends.transcribe_openrouter`** — OpenAI-format `/audio/transcriptions` for diarized STT (the `transcribe` stage).
- **`transcriber.backends.openrouter`** — shared chat-completions helper used by the slides `openrouter` backend (`describe_slides`).
- **`transcriber.backends.slides.OpenRouterSlidesBackend`** — one OpenRouter vision call per slide, via the helper above.

The `agno` summarize backend also touches OpenRouter indirectly: it uses an Agno `OpenRouter` model provider (imported lazily), but the HTTP seam there is owned by Agno, not by the transcriber's shared helpers.

## Why OpenRouter is unconditionally required

The config model treats `openrouter` as a top-level, required section. It is not optional "when slides are enabled" or "when summarizing with agno" — those were earlier triggers. At load time, `transcriber.config._from_dict` hard-fails with a `ConfigError` when the `openrouter` mapping is absent, because it is the transcription backend (OpenRouter STT) in addition to the existing slides/agno triggers.

The relevant validation lives in `transcriber/config.py`:

```python
# openrouter is now UNCONDITIONALLY required: it is the transcription
# backend (OpenRouter STT), in addition to the existing slides/agno
# triggers. Every config without an `openrouter` section raises (R13).
needs_openrouter = True
if needs_openrouter and openrouter is None:
    raise ConfigError(
        "'openrouter' section is required: it is the transcription backend "
        "(OpenRouter STT), and is also required when slides are enabled "
        "(stages.slides.enabled) or summary.backend == 'agno'"
    )
```

That single check is why a host config missing `openrouter` cannot even start the orchestrator.

## Configuration model

The `openrouter` section is described by `transcriber.config.OpenRouter`:

- `api_key_env` (required) — env-var **name** for the API key, not the key itself.
- `base_url` (default `https://openrouter.ai/api/v1`) — the OpenAI-format root URL both seams use.
- `slides_model` (default `google/gemini-2.0-flash-001`) — the vision model used by `describe_slides`.
- `summary_model` (default `google/gemini-2.5-pro`) — the tool-capable model used by the `agno` summarize path.
- `max_slides` (default `60`) — deterministic slide-count ceiling for the `openrouter` slides backend.

The `Transcribe` stage block is separate from `OpenRouter`:

- `transcribe.model_id` (default `microsoft/mai-transcribe-2`) — the STT model slug sent to `/audio/transcriptions`.
- `transcribe.diarize`, `transcribe.segment_seconds`, `transcribe.overlap_seconds` — chunking and diarization knobs.

This separation matters: the STT model and the vision/summary models are selected by different config fields and belong to different stages. Do not conflate `transcribe.model_id` with `openrouter.slides_model` or `openrouter.summary_model`.

## API key handling

Secrets are never stored in the config model. The config references secrets by environment-variable **name only**, and the actual value is resolved at **use time** via `transcriber.config.resolve_env`.

`resolve_env` reads `os.environ.get(var_name)` and raises `MissingEnvVarError` when the variable is absent, with a message naming the variable. It is the single secret-resolution seam used by both the transcription and slides paths.

At the HTTP seams, the key is resolved immediately before building the request:

- `transcriber.backends.transcribe_openrouter._openrouter_transcribe` resolves `config.openrouter.api_key_env` and places it in `Authorization: Bearer <key>`.
- `transcriber.backends.openrouter._openrouter_vision` does the same for the chat-completions path.

Both seams then attach the public, non-secret OpenRouter attribution headers (`HTTP-Referer`, `X-Title`) before posting.

The log-hygiene contract is strict and documented in the module docstrings: the API key, request headers, and base64 audio/image payloads are **never logged or embedded in exceptions**. Errors carry only the HTTP status and a short truncated body snippet.

## HTTP seams

### Transcription seam (`/audio/transcriptions`)

`transcriber.backends.transcribe_openrouter._openrouter_transcribe` transcribes exactly one audio file and returns one `TranscriptChunk`. It mirrors the retry/backoff/`_snippet`/log-hygiene scaffolding of the vision seam, but posts a **JSON base64** body rather than OpenAI multipart.

Key behaviors:

- One POST per audio part to `POST {base_url}/audio/transcriptions`.
- The audio is read as bytes and base64-encoded as `input_audio.data` with `format: "mp3"` — a raw base64 payload, **not** a `data:` URI (documented as P3 in the module docstring).
- When `diarize` is true, it requests `verbose_json` and merges a per-model provider diarization block for `microsoft/mai-transcribe-2` (the only model with a registered toggle). An unknown model sends `verbose_json` without a provider toggle and relies on native diarization, failing cleanly on a 400 if unsupported.
- When `language` is set (and recognized), it is sent; otherwise it is omitted for auto-detection.
- Retry behavior:
  - `429`, `500`, `502`, `503`, `504` are retried with bounded exponential backoff (up to `MAX_ATTEMPTS = 4` total tries including the first).
  - A `400` is **not** retryable: it fails immediately with no silent fallback (R10).
  - Transport errors (`httpx.HTTPError`) are also retried within the cap.
  - Any other non-2xx after retries raises `TranscribeError`.
- Response parsing is defensive: `_parse_response` prefers `segments`, falls back to `words` for per-speaker turns, and degrades to flat `raw_text` with a warning when diarization yields no parseable shape (R5). It never silently crashes.

The manifest in `transcribe_recording` makes this seam partially idempotent: completed parts are read from `segments.json` and never re-issued, so a retry after a failure does not re-pay already-completed segments.

### Vision seam (`/chat/completions`)

`transcriber.backends.openrouter._openrouter_vision` is the shared helper used by the slides `openrouter` backend. It:

- Posts to `POST {base_url}/chat/completions` with the caller-supplied `messages` and `model`.
- Honors the per-request `timeout`.
- Retries `429` and `5xx` with bounded exponential backoff, then raises `SlideDescribeError`.
- On a non-2xx after retries, malformed JSON, or a missing `choices[0].message.content`, raises `SlideDescribeError`.

The vision seam is intentionally smaller than the transcription seam because it has no payload encoding, no diarization, and no chunk orchestration — it is a single typed chat-completion call.

## Per-stage model selection

Model selection is a pure function of config, and each stage picks from a different field:

- **`describe_slides`** always uses the `openrouter` vision backend (Spec R19). The orchestrator's `_get_slides_backend` validates at config load that `stages.slides.backend == "openrouter"` and returns `OpenRouterSlidesBackend()`. The model sent per slide is `config.openrouter.slides_model`.
- **`transcribe`** always uses the OpenRouter STT seam. The model sent per audio part is `config.transcribe.model_id`.
- **`summarize`** selects `agy` (default) or `agno`. When `agno`, the summary runs through an Agno `OpenRouter` model using `config.openrouter.summary_model`. That import is lazy and the HTTP seam is owned by Agno, not by the shared helpers.

The allowed backend selectors are constrained at config load:

- `SLIDES_BACKENDS = ("openrouter",)`
- `SUMMARY_BACKENDS = ("agy", "agno")`

An unknown value raises `ConfigError` at load time rather than falling back silently.

## Slides path specifics

The `describe_slides` backend issues **one OpenRouter vision call per slide**, each carrying a single downscaled JPEG and the `Timestamp: MM:SS - MM:SS` text part placed immediately before the `image_url` part.

Important invariants:

- The descriptor is **image-only**: it never receives the transcript. Transcript cross-referencing is the summarize stage's job.
- Empty semantics (R9): an empty slide set returns `""` with **no** HTTP call; a valid empty/whitespace backend response contributes nothing.
- Per-slide timestamps (R8): unknown timing is passed as the literal `unknown`, never omitted.
- Downscale (R25): each JPEG is downscaled to a bounded long edge (`MAX_IMAGE_LONG_EDGE = 1024`) before base64. This is a size/cost control, not redaction.
- Deterministic ceiling (R23): module constants cap the max slide count and the max total encoded bytes; exceeding either raises `SlideDescribeError` **before** any HTTP call. Over-ceiling decks hard-fail (no chunking).

The ceiling check is performed once up front by `_preflight_deck_encoded_bytes`, which encodes every slide once into a cache and enforces the whole-deck encoded-byte bound before any image is sent. The per-slide message builder reuses that cache so images are never re-encoded.

Log hygiene for the slides path is also strict: logs carry stage/model/slide-count/elapsed only, never the API key, headers, or image bytes.

## Notion publishing is not part of the OpenRouter HTTP seam

The `agno` summarize backend produces the Markdown summary with an OpenRouter model, but the actual Notion publish is **engine-side**, not model-side. The engine publishes the subpage directly via the Notion REST API (`transcriber.backends.notion_publish.publish_to_notion`), with no MCP and no model tool-calling.

This split exists because the official Notion MCP's tool schemas break tool-calling on Gemini/Mistral over OpenRouter (empty `null` completions, zero tool calls). So:

- `agy` publishes via its own MCP as part of its run.
- `agno` publishes engine-side via the Notion REST API.

The Notion token is resolved at use time from `config.notion.token_env` and is never logged, mirroring the OpenRouter key handling.

## Retry, timeout, and partial-failure behavior

Timeouts are configured per-stage in `transcriber.config.Timeouts`, defaulting to `DEFAULT_TIMEOUT_SECONDS = 900`:

- `ffmpeg`, `scenedetect`, `slides`, `transcribe`, `agy`, `s3` each have their own field.
- `summarize` is optional; `None` falls back to `agy` at use time (used by the `agno` backend).

The two OpenRouter seams share the same retry/backoff constants:

- `MAX_ATTEMPTS = 4`
- `_BACKOFF_BASE_SECONDS = 0.5`
- `_BACKOFF_CAP_SECONDS = 8.0`
- Retryable statuses: `429`, `500`, `502`, `503`, `504`

Failure isolation is orchestrated, not at the HTTP seam: a raised exception in one stage/recording is caught, logged, recorded in that recording's manifest, and the batch continues. Cleanup runs only on full success.

For the `transcribe` stage specifically, the `segments.json` manifest gives per-segment idempotency and a parameter guard: if the stored `segment_seconds`/`overlap_seconds` differ from the current config, the work dir is discarded and rechunked (E5).

## Log hygiene and secrets

The integration enforces a consistent log-hygiene contract across both seams and the surrounding backends:

- The OpenRouter API key is resolved at use time from an env-var name and is never stored in the config model.
- The API key, request headers, and base64 payloads (audio or image) are never logged and never embedded in exceptions.
- Error messages carry only the HTTP status and a short truncated body snippet (bounded by `_SNIPPET_LEN = 200`).
- Transport exceptions are not logged with their request, because the request carries the headers and base64 payload; only the exception type name is logged.

## Files that matter

The integration is mostly concentrated in a small set of backend and config modules:

- `transcriber/config.py` — `OpenRouter`, `Transcribe`, `Timeouts`, `Config`, `resolve_env`, and the unconditional `openrouter` requirement.
- `transcriber/backends/transcribe_openrouter.py` — STT seam, chunker, stitch/reconcile/render, manifest, and stage entry.
- `transcriber/backends/openrouter.py` — shared vision chat-completions helper.
- `transcriber/backends/slides.py` — `OpenRouterSlidesBackend`, slide-input helper, downscaling/encoding, ceiling checks, placeholder recognition.
- `transcriber/backends/errors.py` — `TranscribeError`, `SlideDescribeError`, `SummarizeError`.
- `transcriber/backends/interfaces.py` — `SlidesBackend`, `SummarizeBackend`, `SlideInput`, `SummaryResult`.
- `transcriber/backends/summarize.py` and `transcriber/backends/summarize_agno.py` — summarize backend selection and the `agno` path.
- `transcriber/backends/notion_publish.py` and `transcriber/backends/notion_mcp.py` — Notion publishing (REST for `agno`, MCP for `agy`), which is related but not part of the OpenRouter HTTP seam.

The orchestrator entry point that wires these together is `transcriber/__main__.py`, including `_get_slides_backend` and `_uses_agy`.
