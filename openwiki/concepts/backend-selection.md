---
type: concept
title: Backend selection and interfaces
description: How the transcriber selects describe_slides and summarize backends as pure functions of config, the concrete slides/openrouter and summarize/agno backends, their shared OpenRouter block with per-stage models, and the Notion publish split.
tags: [transcriber, backends, openrouter, notion, agno, slides, summarize]
verified:
  - by: openwiki/0.6.1
    at: 2026-10-06T06:48:26.024Z
sources:
  - id: openwiki-source-df4110b2c5338913ae9eedcf
    resource: repo://transcriber/__main__.py
  - id: openwiki-source-7eaaa16b2c5b3baf4675d042
    resource: repo://transcriber/backends/__init__.py
  - id: openwiki-source-8a3a341e370fcf0ec5b903de
    resource: repo://transcriber/backends/interfaces.py
  - id: openwiki-source-a3b622f6f01eeb4e4042c7e0
    resource: repo://transcriber/backends/notion_publish.py
  - id: openwiki-source-d03ac1f85a9e3bd2b413eec1
    resource: repo://transcriber/backends/slides.py
  - id: openwiki-source-8ae57789b707a6fc3e3769d7
    resource: repo://transcriber/backends/summarize_agno.py
  - id: openwiki-source-5670223669d02f5c4fdf5d64
    resource: repo://transcriber/backends/summarize.py
  - id: openwiki-source-c4777b8db8d4806695ac8b6a
    resource: repo://transcriber/config.py
generated: { by: "openwiki/0.6.1", at: "2026-10-06T06:48:26.024Z" }
---

# Backend selection and interfaces

The two post-transcript stages (`describe_slides` and `summarize`) each select a pluggable backend as a **pure function of config**. Selection happens at orchestrator runtime, not inside the media pipeline. The slides backend is now restricted to `openrouter`, while the summarize backend is now restricted to `agno`, and the two backends differ in how they publish to Notion.

Selection is validated at config load and at the orchestrator call site; an unexpected backend value raises rather than silently falling back. There is **no silent cross-backend fallback** per stage.

## Stage/backend matrix

| Stage | Config selector | Valid backends | Default |
|---|---|---|---|
| `describe_slides` | `config.stages.slides.backend` | `"openrouter"` | `"openrouter"` |
| `summarize` | `config.summary.backend` | `"agno"` | `"agno"` |

The two selections are orthogonal: they do not change each other's behavior, artifacts, or contracts. For example, `slides=openrouter` + `summarize=agno` is the supported combination in the shipped code.

The concrete configs used by each OpenRouter consumer are distinct even though they share one `openrouter` block:

* the slides `openrouter` backend uses `config.openrouter.slides_model` (`google/gemini-2.0-flash-001` by default);
* the summarize `agno` backend uses `config.openrouter.summary_model` (`google/gemini-2.5-pro` by default).

So the shared `openrouter` block is **one block, two per-stage models**.

## Why `agy` is no longer a valid slides backend

The spec originally allowed `slides.backend ∈ {"openrouter"}` with `openrouter` as the default to preserve today's behavior. The shipped engine made a deliberate scope decision: the slides stage is now validated to accept only `"openrouter"`, and config now defaults `stages.slides.backend` to `"openrouter"`.

That means:

* `describe_slides` always runs on the `openrouter` vision backend.
* The `agy` summarize backend was the original post-transcript backend and remains the canonical `agy` path; the shipped engine's selected summarize backend is `agno` via `get_summarize_backend`.
* The old "both default to `agy`" expectation no longer holds; dropping `agy` from the slides stage is what made `slides=openrouter` the current default and the only valid slides selection.

This is why the page says `agy` is no longer a valid slides backend: the project spec originally listed `agy` as one of two possible slides backends, but the implementation selected `openrouter` as the only slides backend and removed `agy` from that stage entirely.

## Why Notion MCP is still required for summarize

Summarize is not a bare OpenRouter call because the stage must publish to Notion, and the engine's summarize backend is the one that can do that. In the final implementation that is true but the Notion path is backend-specific:

* **`agy`** — the original post-transcript backend publishes via `agy`'s own Notion MCP as part of its run; the engine does not own that path.
* **`agno`** — the **engine** publishes the subpage directly via the Notion REST API (`transcriber.backends.notion_publish.publish_to_notion`); no MCP, no model tool-calling.

The project doc's "Notion MCP is non-negotiable for summarize" intent still holds in spirit — every summarize run publishes to Notion or fails the stage — but the concrete implementation replaced the `agno` Notion-MCP plan with a direct REST publish because the official Notion MCP's `oneOf`/`anyOf`/`$ref` tool schemas break tool-calling on Gemini/Mistral over OpenRouter (empty `null` completions, zero tool calls). The `agy` backend was left unchanged and still publishes via MCP.

That is also why the engine does **not** hand-roll a Notion REST client for the summarize stage in the `agy` path: `agy`'s Notion work is part of the `agy` run itself. The engine only owns a direct REST publish for the `agno` path, as a deliberate swap for the unreliably-model-driven MCP path.

## Interfaces

`transcriber.backends.interfaces` defines two small, pure `Protocol` interfaces selected per-stage by config:

* `SlidesBackend` — turns extracted slide images (+ transcript context) into a `<name>.slides.md` markdown block.
* `SummarizeBackend` — turns the transcript (+ the slide markdown) into `<name>.md` + `<name>.telegram.md` and publishes to Notion.

Shared data types:

* `SlideInput` — a single extracted slide plus its scene timing (`image_path`, `timestamp`). `timestamp` is `MM:SS - MM:SS` or the literal `"unknown"`; timing is never omitted.
* `SummaryResult` — paths to the two files a summarize backend writes (`summary_path` = `<name>.md`, `telegram_path` = `<name>.telegram.md`). Both files are written by the backend; the engine reads them back (file-is-source-of-truth contract).

Method contracts:

* `SlidesBackend.describe(slides, transcript_text, config, *, timeout) -> str`
* `SummarizeBackend.summarize(transcript_path, slides_markdown, recording_dir, config) -> SummaryResult`

## Shared error types

`transcriber.backends.errors` owns the shared backend error types:

* `SlideDescribeError` — raised when the `describe_slides` backend fails (transport, non-2xx after retries, malformed JSON, missing `choices[0].message.content`). A deliberately empty backend result is **not** an error.
* `SummarizeError` — raised when the `summarize` backend fails.
* `TranscribeError` — raised when the `transcribe` stage's OpenRouter STT backend fails.

All three follow the same log-hygiene contract: error messages carry only a short, non-sensitive context (HTTP status + truncated body snippet), never the API key, request headers, or base64 image/audio bytes.

## Slides backend (openrouter only)

`transcriber.backends.slides` implements the `describe_slides` backend. `openrouter` is the only slides backend.

`OpenRouterSlidesBackend` issues **one OpenRouter vision call per slide** via the shared `_openrouter_vision` helper, turning each extracted slide JPEG into part of the `<name>.slides.md` markdown block. The descriptor is **image-only**: it never receives the transcript. Transcript cross-referencing is the summarize stage's job.

`build_slide_inputs` lists the extracted slide JPEGs in a deterministic order and joins each to its scene timing parsed from the scenes CSV that scenedetect writes **into the slides dir** (`extracted_slides.<name>/<name>.scenes.csv`), never a `.mp4` sibling.

Design invariants:

* **Empty semantics** — an empty slide set → `""` with **no** HTTP call; a valid empty/whitespace backend response → `""` (not an error). A transport/format failure raises `SlideDescribeError`.
* **One call per slide** — each slide is sent in its own vision call carrying a single image and no transcript; the per-slide markdown is concatenated.
* **Per-slide timestamps** — each slide's message carries a `Timestamp: MM:SS - MM:SS` text part placed **immediately before** its `image_url` part. Unknown timing is passed as the literal `unknown` — never omitted.
* **Downscale** — each JPEG is downscaled to a bounded long edge (`MAX_IMAGE_LONG_EDGE = 1024`) before base64 to control size/cost. This is a size control, **not** redaction.
* **Deterministic ceiling** — module constants cap the max slide count (`MAX_SLIDES = 60`, overridable via `openrouter.max_slides`) and the max total encoded bytes (`MAX_TOTAL_ENCODED_BYTES = 48 MiB`); exceeding either raises `SlideDescribeError` **before** any HTTP call. Over-ceiling decks hard-fail (no chunking).
* **Log hygiene** — logs carry stage/model/slide-count/elapsed only — never the API key, headers, or image bytes.
* **No pipeline coupling** — the backend is not called from `pipeline.process_recording` — the pipeline stays ffmpeg + scenedetect + transcribe only. It runs in the manifest-gated orchestrator step.

The slides backend no longer *drops* empty slides — it keeps the model's output (markers included) in `<name>.slides.md` so empty scenes stay identifiable. Stripping markers + stray placeholders happens later, in `transcriber.agent.append_slide_descriptions`, only for the summary / Notion output.

## Summarize backends (agy / agno)

`transcriber.backends.summarize` implements two backends:

* `AgySummarizeBackend` — drives the local `agy` CLI agent exactly as today (`transcriber.agent.run_agent`): writes `<name>.md` + `<name>.telegram.md` and publishes to Notion via `agy`'s own MCP. Behavior is unchanged from the pre-split engine.
* `AgnoSummarizeBackend` — lives in `transcriber.backends.summarize_agno` and is imported **only when selected** (so the optional `agno` extra is not a base dependency). It produces the summary + digest with an Agno/OpenRouter model, then the **engine** publishes the Notion subpage directly via the Notion REST API (`transcriber.backends.notion_publish.publish_to_notion`) — no MCP, no model tool-calling. Use `get_summarize_backend` to obtain the configured backend without importing `agno` on the default path.

Both backends produce the same artifacts, but the Notion publish path is **backend-specific**: `agy` publishes via its own MCP, while `agno` publishes engine-side via the Notion REST API. Selection is a pure function of `config.summary.backend`; there is **no silent cross-backend fallback**.

`get_summarize_backend` returns `AgySummarizeBackend()` for `"agy"` (without importing `agno`), and lazily imports `AgnoSummarizeBackend` for `"agno"`. An unknown value raises.

The `agno` backend's hard time bound is the summarize-stage timeout: `timeouts.summarize` when set, otherwise `timeouts.agy`. The blocking Notion publish runs in a worker thread so the timeout can still cancel the run. After the run, the engine re-uses the same non-empty `<name>.md` success check as the `agy` path: an empty or absent summary file fails the stage (retryable). Summarize + Notion publish happen in a single run, so a Notion failure fails the whole `summarize`+`notion` stage, like `agy`.

## Notion MCP (agy path) vs Notion REST (agno path)

For the `agy` summarize backend, Notion is published by `agy` itself through its MCP inside the agent run. The engine does not own that publish path.

For the `agno` summarize backend, the engine publishes directly via the Notion REST API. The direct REST path:

1. Extracts the parent **page id** from `notion.parent_page_id` (accepts a bare id — hyphenated or not — or a full Notion URL with the id embedded).
2. Converts the Markdown summary into Notion **block** objects, **losing nothing**: headings, paragraphs, bullet/numbered list items, blockquotes and fenced code blocks map to their native block types; anything else falls back to a paragraph block so all text survives.
3. Creates a new subpage under the parent (title = the recording basename) with up to the first 100 blocks, then appends the remainder in `<=100`-block chunks (Notion caps children per request at 100).
4. When cleaned slide descriptions are supplied, creates a child page titled "Slide Descriptions" **under the summary page** holding the slide blocks, so the summary page stays short and the long slide detail lives one click away.

Secrets: the Notion token is resolved by env-var *name* (`notion.token_env`) at use time and is **never logged**.

## Orchestrator selection

The orchestrator in `transcriber.__main__` selects backends as pure functions of config:

* `_get_slides_backend(config)` returns `OpenRouterSlidesBackend()` when `config.stages.slides.backend == "openrouter"`, and raises `ValueError` otherwise.
* `get_summarize_backend(config)` returns the configured summarize backend.
* `_uses_agy(config)` returns `True` only when a stage actually uses `agy` (summarize `agy`); the slides stage never uses `agy`.

The dry-run plan and `transcriber check` surface the selected backend per stage, including the per-stage OpenRouter model where relevant (for example `transcribe [openrouter:<model_id>]` and `describe-slides [<backend>]` / `summarize+notion [<backend>]`).

## Relationship to the transcribe stage

The `transcribe` stage is a separate OpenRouter consumer. It is **not** a slides or summarize backend; it runs inside the media pipeline (`pipeline._transcribe`) and uses the OpenRouter `/audio/transcriptions` seam via `transcriber.backends.transcribe_openrouter`. It shares the same `openrouter` config block (for `base_url` and `api_key_env`) but uses `config.transcribe.model_id` for its own model, distinct from the slides and summarize models.

## Related

* [OSS Companion Transcriber Engine](../architecture/oss-companion-transcriber.md)
* [Config Model](../concepts/config-model.md)
* [Notion integration](../integrations/notion.md)
* [OpenRouter integration](../integrations/openrouter.md)
