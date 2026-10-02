---
type: workflow
title: Slide Description Workflow
description: How describe_slides builds slide inputs from extracted JPEGs and a scenes CSV, runs the openrouter vision backend one call per slide, writes <name>.slides.md, and is gated by the state manifest with ceiling and idempotency guards.
tags: [transcriber, slides, describe_slides, openrouter, vision, manifest, idempotency, ceiling, scenedetect]
verified:
  - by: openwiki/0.6.1
    at: 2026-10-02T19:10:14.922Z
sources:
  - id: openwiki-source-df4110b2c5338913ae9eedcf
    resource: repo://transcriber/__main__.py
  - id: openwiki-source-209e1f0671f9315f4a7eb9c7
    resource: repo://transcriber/backends/openrouter.py
  - id: openwiki-source-d03ac1f85a9e3bd2b413eec1
    resource: repo://transcriber/backends/slides.py
generated: { by: "openwiki/0.6.1", at: "2026-10-02T19:10:14.922Z" }
---

# Slide Description Workflow

The `describe_slides` stage is the manifest-gated post-transcript step that turns extracted slide JPEGs into a `<name>.slides.md` markdown block by calling the OpenRouter vision API once per slide. It is the only stage whose backend is locked to `openrouter`, and it runs strictly after the deterministic media pipeline (`pipeline`) has produced the slide images and scenes CSV.

**Related pages:** [OSS Companion Transcriber Engine](../architecture/oss-companion-transcriber.md), [Backend Selection and Interfaces](../concepts/backend-selection.md), [OpenRouter Integration](../integrations/openrouter.md), [Pre-flight Checks and Idempotent Retries](../operations/pre-flight-and-idempotency.md).

## Position in the pipeline

The transcriber runs stages in this order for each recording:

1. **pipeline** — ffmpeg audio extract, optional scenedetect slide extraction, OpenRouter STT transcription. Media-only; no slides or summarize backend calls.
2. **describe_slides** — (manifest-gated) the OpenRouter vision backend produces `<name>.slides.md` from the extracted JPEGs.
3. **summarize + notion** — consumes the transcript and, if present, the slide markdown; publishes to Notion.
4. **telegram** — sends the digest or full summary.
5. **s3** — optional S3 sync of intermediates.
6. **cleanup** — deletes intermediates on full success (unless kept).

The `describe_slides` stage is the only stage whose backend is restricted to `openrouter`. The summarize stage separately selects `agy` (default) or `agno` — those two selections are orthogonal and do not affect each other.

## Inputs: what the stage consumes

The stage consumes three things, assembled before any OpenRouter call:

- **Slide JPEGs** — extracted by the pipeline's scenedetect step into `extracted_slides.<name>/` beside the `.mp4` (when `config.stages.slides.enabled`).
- **Scenes CSV** — `extracted_slides.<name>/<name>.scenes.csv`, written by scenedetect **into the slides dir**, not beside the `.mp4`. This is where `build_slide_inputs` reads it (E7).
- **Transcript text** — read from `<name>.txt` by the orchestrator and passed to the backend's `describe` method, but **unused** by the slides backend: the descriptor is image-only.

## The decision flow in the orchestrator

<!-- openwiki: broken internal link [/transcriber/__main__.py] link "/transcriber/__main__.py" is root-absolute, which no real consumer resolves against the repository root (not a coding agent reading the page, not GitHub's Markdown renderer, not a local viewer); use a path relative to this file instead. Fix the href or restore the target, then delete this comment. -->
The orchestrator (`_process_one` in [`/transcriber/__main__.py`](/transcriber/__main__.py)) decides whether to run the slides backend at all. The decision has three skip paths before any paid call:

<!-- openwiki: mermaid parse failed and this diagram was converted to a text fence so it does not break rendering. Fix the diagram source and restore the mermaid fence. Parser error: Heuristic: an unescaped angle bracket inside a label breaks rendering; rephrase the label. -->
```text
flowchart TD
    StartEntry["Orchestrator: describe_slides stage"] --> CheckEnabled{"slides.enabled?"}
    CheckEnabled -->|false| SkipDisabled["log: disabled, skip"]
    CheckEnabled -->|true| CheckManifest{"manifest.is_complete(describe_slides)?"}
    CheckManifest -->|true| SkipManifest["log: already complete, skip"]
    CheckManifest -->|false| CheckArtifact{"<name>.slides.md exists?"}
    CheckArtifact -->|true| IdempotentSkip["idempotent skip: artifact present, mark complete, no paid call"]
    CheckArtifact -->|false| RunBackend["select backend, build_slide_inputs, call backend.describe, write <name>.slides.md, mark complete"]
    SkipDisabled --> ExitStage["stage done"]
    SkipManifest --> ExitStage
    IdempotentSkip --> ExitStage
    RunBackend --> ExitStage
```

**Caption:** The describe_slides decision flow in the orchestrator. Three skip paths (disabled, manifest-complete, artifact-present) protect against redundant paid calls; only the last path issues the OpenRouter vision calls.

The artifact-present idempotent skip is important: if a prior run wrote `<name>.slides.md` but crashed before marking `describe_slides` complete, the next run sees the file, skips the paid backend call, and marks the stage complete. This avoids re-paying for the same deck.

## Building slide inputs: `build_slide_inputs`

<!-- openwiki: broken internal link [/transcriber/backends/slides.py] link "/transcriber/backends/slides.py" is root-absolute, which no real consumer resolves against the repository root (not a coding agent reading the page, not GitHub's Markdown renderer, not a local viewer); use a path relative to this file instead. Fix the href or restore the target, then delete this comment. -->
`build_slide_inputs(recording_dir, name)` lives in [`/transcriber/backends/slides.py`](/transcriber/backends/slides.py) and is the helper that turns extracted files into an ordered list of `SlideInput` objects.

### What it does

1. Locates the slides dir: `extracted_slides.<name>/` under the recording dir.
2. Lists slide JPEGs (`.jpg`/`.jpeg`) in **sorted-by-name** order — deterministic, so re-runs produce the same sequence.
3. Reads the scenes CSV at `extracted_slides.<name>/<name>.scenes.csv` — **inside the slides dir**, not beside the `.mp4` (E7). This matters because scenedetect writes it there.
4. Parses the CSV into ordered `MM:SS - MM:SS` range strings. The parser locates the header row by its `Start Timecode` / `End Timecode` columns, skipping a leading cut-list row, so it is robust to scenedetect's default CSV shape.
5. Joins each image to a timestamp. When the CSV is missing, unparseable, or has fewer rows than images, the affected slides get the literal `"unknown"` and a warning is logged so the degraded path is visible (R8).

### Why the CSV lives in the slides dir

The scenes CSV is co-located with the extracted images inside `extracted_slides.<name>/` because that is where scenedetect writes it. The pipeline's `_extract_slides` passes a `scenes_csv` path that points there. `build_slide_inputs` reads it from the same location, so the slide-input builder and the extractor agree on where timing metadata lives. This avoids a second copy or a search across the recording dir.

### `SlideInput` shape

<!-- openwiki: broken internal link [/transcriber/backends/interfaces.py] link "/transcriber/backends/interfaces.py" is root-absolute, which no real consumer resolves against the repository root (not a coding agent reading the page, not GitHub's Markdown renderer, not a local viewer); use a path relative to this file instead. Fix the href or restore the target, then delete this comment. -->
`SlideInput` is a small dataclass defined in [`/transcriber/backends/interfaces.py`](/transcriber/backends/interfaces.py):

- `image_path: Path` — path to the slide JPEG.
- `timestamp: str` — `MM:SS - MM:SS` scene range, or the literal `"unknown"`. Timing is **never omitted** (R8).

## The backend: `OpenRouterSlidesBackend`

`OpenRouterSlidesBackend` is the only slides backend. It is returned by `_get_slides_backend(config)` when `config.stages.slides.backend == "openrouter"`; any other value raises `ValueError` (Spec R19). Selection is validated at config load and at the orchestrator call site.

### What the backend does

`describe(slides, transcript_text, config, *, timeout) -> str`:

1. **Empty deck → `""`, no HTTP call.** If `slides` is empty, log and return empty immediately. No network request is made (R9).
2. **Config guard.** If `config.openrouter` is `None`, raise `SlideDescribeError` — the backend requires the OpenRouter section (R13/R16).
3. **Slide-count ceiling check (R23).** Read `max_slides` from `config.openrouter.max_slides` (defaults to the module constant `MAX_SLIDES = 60`). If `len(slides) > max_slides`, raise `SlideDescribeError` **before any HTTP call**. An over-ceiling deck hard-fails; the backend does not chunk or truncate (R23).
4. **Whole-deck encoded-byte ceiling (R23).** Encode every slide once into a `{image_path: base64}` cache via `_preflight_deck_encoded_bytes`, summing encoded bytes as it goes. If the total exceeds `MAX_TOTAL_ENCODED_BYTES = 48 MiB`, raise `SlideDescribeError` **before any HTTP call**. The cache is reused per slide so images are never re-encoded. This is a whole-deck bound, not a per-slide bound — a deck can trip even when each individual slide is under the ceiling.
5. **One vision call per slide.** For each slide, build a single user message with:
   - The leading text (extractor rules + language instruction — image-only, no transcript).
   - A `Timestamp: MM:SS - MM:SS` text part placed **immediately before** the image part. Unknown timing is the literal `unknown`.
   - One `image_url` part carrying the downscaled base64 JPEG.
   
   The message is sent via `_openrouter_vision`, which POSTs to `chat/completions` on the OpenRouter HTTP seam with retry/backoff for `429` and `5xx`, and raises `SlideDescribeError` on non-2xx after retries, malformed JSON, or missing `choices[0].message.content`.
6. **Collect results.** Each non-empty response is appended (verbatim, including any `[[SLIDE_EMPTY ...]]` marker or stray placeholder). Empty/whitespace responses contribute nothing. Placeholder responses are logged but **kept** in the output for debugging (R9 semantics: empty slides are not dropped).
7. **Return.** The per-slide markdown is joined with `\n\n`. If every slide returned empty/whitespace, the result is `""` — that is not an error.

### Image-only descriptor

The slide descriptor is **image-only**: it never receives the transcript. The `transcript_text` parameter is accepted to satisfy the `SlidesBackend` protocol and the orchestrator call site, but is `del`'d inside `describe`. Transcript cross-referencing (speaker commentary, decisions, Q&A) is the summarize stage's job, which receives the full transcript plus this slide markdown.

### Per-slide message shape

Each slide's message is a single user message with three content parts in order:

1. `{"type": "text", "text": <leading_text>}` — extractor rules + language instruction.
2. `{"type": "text", "text": f"Timestamp: {slide.timestamp}"}` — placed **immediately before** the image part. Unknown timing is `"unknown"`, never omitted.
3. `{"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}}` — the downscaled base64 JPEG.

The model used is `config.openrouter.slides_model` (default `google/gemini-2.0-flash-001`), **not** `config.openrouter.summary_model`. The shared `openrouter` block is one block with two per-stage models.

### Downscale (R25)

Each JPEG is decoded, downscaled to at most `MAX_IMAGE_LONG_EDGE = 1024` px on the long edge (using OpenCV, a transitive dependency via scenedetect), re-encoded as JPEG at quality 85, then base64-encoded. This is a **size/cost control, not redaction**: the full visual content is preserved, just at a bounded resolution to control payload size and API cost.

### Log hygiene (R16/R17)

Logs carry only stage/model/slide-count/elapsed/encoded-bytes. The API key, request headers, and image bytes are **never logged**. The shared `_openrouter_vision` helper follows the same contract: error messages carry only HTTP status + a short truncated body snippet, never the key or base64 payload.

## Ceiling behavior and the over-ceiling hard-fail guard

The slides backend has two deterministic ceilings, both enforced **before any HTTP call** (R23):

| Ceiling | Constant / config | Default | Effect when exceeded |
|---|---|---|---|
| Slide count | `openrouter.max_slides` (defaults to `MAX_SLIDES = 60`) | 60 | `SlideDescribeError` raised before any call; deck hard-fails, no chunking |
| Total encoded bytes | `MAX_TOTAL_ENCODED_BYTES = 48 MiB` (module constant) | 48 MiB of base64 image data | `SlideDescribeError` raised before any call; deck hard-fails |

Both are **whole-deck** bounds: the slide-count ceiling checks `len(slides)` once; the byte ceiling encodes every slide in a single pass and sums the encoded lengths. A deck that exceeds either ceiling raises `SlideDescribeError` before a single image is sent — the caller cannot accidentally pay for part of a deck that will later be rejected.

The host can raise the slide-count ceiling via `openrouter.max_slides` in config. The byte ceiling is a module constant with no config override; an over-byte-deck host must raise the ceiling (by forking the module constant), split the recording, or disable the slides stage.

**Important:** exceeding a ceiling is a hard failure, not a retryable transient. The deck is rejected wholesale; there is no chunking, truncation, or partial-send fallback.

### Downscale is not redaction

The downscale step controls size and cost by bounding the long edge to 1024 px. It does not remove, crop, or obscure content. A reader cannot interpret the downscale as any form of content redaction or sanitization; it is purely a payload-size control. The full slide is still sent, just at a bounded resolution.

## What `<name>.slides.md` contains

The written file is the raw concatenation of per-slide model output, joined by `\n\n`. It includes:

- Real slide descriptions (headings, bullets, tables, mermaid blocks) for slides with essential visual content.
- `[[SLIDE_EMPTY <timestamp>]]` marker lines for empty scenes — **kept verbatim**, not dropped, so empty scenes stay identifiable for debugging.
- Stray placeholder phrases a disobedient model may emit (e.g. `== no information ==`, `<!-- no information -->`) — also kept in `.slides.md`.

The slides backend no longer drops empty slides. Stripping of markers and stray placeholders happens **later**, in `transcriber.agent.append_slide_descriptions`, only for the summary/Notion output. The `<name>.slides.md` file is the markers-kept debug intermediate; its cleaned counterpart `<name>.lides-clean.md` is what survives cleanup.

## Placeholder recognition

The module recognizes placeholder slide responses so it can log them (and so the later stripping step can remove them from summary output). The recognizer `is_placeholder_slide_response(content)` detects three forms:

1. The exact `[[SLIDE_EMPTY ...]]` marker (the instructed convention).
2. A single short line matching a known placeholder phrase (e.g. `none`, `== no information ==`, `<!-- no information -->`).
3. A single-line apologetic placeholder that **leads** with a specific `no essential/visual …` phrase AND is parenthetical or mentions "omit".

Multi-line real descriptions are always kept. Bare phrases like `"no information"` match by exact equality only, so a real sentence such as `"no information was lost during the migration"` is kept.

## Interaction with the state manifest

The manifest (`<name>.transcriber_state.json`) is the source of truth for stage completion (E1). `describe_slides` is tracked as a stage in the canonical ordered tuple:

```
pipeline, describe_slides, summarize, notion, telegram, s3, cleanup
```

The manifest governs the slides stage in three ways:

1. **Skip when complete.** If `state.is_complete("describe_slides")` is true, the orchestrator skips the backend call entirely.
2. **Idempotent artifact-present skip.** If `<name>.slides.md` already exists (e.g. from a prior crash after write but before mark), the orchestrator skips the paid call and marks complete. This is the guard that prevents re-paying when a crash happened between write and mark.
3. **Pre-describe_slides manifests are incomplete.** Manifests written before `describe_slides` existed as a tracked stage simply lack that key. Because completion is read as `_completed.get(stage, False)`, an absent `describe_slides` is treated as **incomplete**, so re-running re-issues the slides call exactly once and then records it.

`RecordingState` persists after each `mark_complete(stage)` call, so a crash between stages leaves an accurate manifest for the next retry. On full success, the orchestrator calls `state.delete()`.

## No pipeline coupling

The slides backend is **not** called from `pipeline.process_recording`. The pipeline stays ffmpeg + scenedetect + transcribe only. The slides backend runs in the manifest-gated orchestrator step (`_process_one`). This is tested explicitly: tests monkeypatch `_openrouter_vision` and `OpenRouterSlidesBackend.describe` to raise on any call, then run the pipeline and assert those never fire — the pipeline produces artifacts without any slides-backend involvement.

## Configuration

The slides stage is configured by two pieces:

- `config.stages.slides.enabled` — bool; when false, the stage is skipped entirely and never writes a slides markdown file.
- `config.stages.slides.backend` — must be `"openrouter"`; validated at config load and at `_get_slides_backend`.

The OpenRouter section supplies the shared HTTP seam and per-stage models:

- `openrouter.api_key_env` — env-var name for the API key (resolved at use time, never stored).
- `openrouter.base_url` — defaults to `https://openrouter.ai/api/v1`.
- `openrouter.slides_model` — the vision model for describe_slides (default `google/gemini-2.0-flash-001`).
- `openrouter.summary_model` — the tool-capable model for the agno summarize path (default `google/gemini-2.5-pro`); **not** used by describe_slides.
- `openrouter.max_slides` — optional override for the slide-count ceiling (defaults to 60).

The slide descriptor uses `config.summary.language` (via `_language_instruction`) to decide the language of the descriptions, but the transcript itself is never passed.

## Stage timeout

The describe_slides stage is time-bounded by `config.timeouts.slides`, read in the orchestrator and passed as the `timeout` parameter to `backend.describe`. The per-slide vision calls each honor that timeout via the shared `_openrouter_vision` helper.

## Error semantics

- **Empty slide set** → `""`, no HTTP call, not an error.
- **Empty/whitespace backend response for a slide** → contributes nothing; not an error.
- **Transport/format failure** (non-2xx after retries, malformed JSON, missing content, missing openrouter config) → `SlideDescribeError`.
- **Over-ceiling deck** (slide count or total encoded bytes) → `SlideDescribeError` raised **before** any HTTP call; hard-fail, no chunking.

<!-- openwiki: broken internal link [/transcriber/backends/errors.py] link "/transcriber/backends/errors.py" is root-absolute, which no real consumer resolves against the repository root (not a coding agent reading the page, not GitHub's Markdown renderer, not a local viewer); use a path relative to this file instead. Fix the href or restore the target, then delete this comment. -->
`SlideDescribeError` is defined in [`/transcriber/backends/errors.py`](/transcriber/backends/errors.py) and follows the log-hygiene contract: error messages carry only a short, non-sensitive context (HTTP status + truncated body snippet), never the API key, request headers, or base64 image bytes.

## Deterministic ordering

Two things are deterministic and must stay that way:

1. **Slide image order** — `build_slide_inputs` lists JPEGs sorted by filename, so re-runs produce the same sequence.
2. **Scenes CSV parse order** — `_parse_scenes_csv` returns ranges in CSV row order, which is the scene order scenedetect produced. Each image is joined to the range at the same index; when there are more images than rows, the extras get `"unknown"`.

This deterministic ordering is what makes the per-slide markdown concatenation stable across re-runs, and what makes the ceiling checks (which iterate the slides once) predictable.

## Module constants

The slides module exposes these constants that govern behavior:

- `MAX_IMAGE_LONG_EDGE = 1024` — max long edge for downscaling (size/cost control, not redaction).
- `_JPEG_QUALITY = 85` — JPEG re-encode quality after downscale.
- `MAX_SLIDES = 60` — default slide-count ceiling; overridable via `openrouter.max_slides`.
- `MAX_TOTAL_ENCODED_BYTES = 48 * 1024 * 1024` (48 MiB) — whole-deck encoded-byte ceiling; module constant, no config override.
- `_SCENES_CSV_SUFFIX = ".scenes.csv"` — filename suffix of the scenes CSV inside the slides dir.
- `_TEMPLATES_DIR` — shared with the agent stage; holds `slide_extractor.md`.

## Prompts

The extractor rules come from `prompt_templates/slide_extractor.md`, read at backend runtime by `_read_extractor_rules()`. The prompt instructs the model to:

- Describe only what is visually present on the slide.
- Output 1-2 bullet points capturing the core thesis.
- Extract tabular data/metrics into a clean Markdown table.
- Convert diagrams/architectures/flows into editable Mermaid.js code blocks.
- Mark empty scenes with exactly one `[[SLIDE_EMPTY <timestamp>]]` line and nothing else — no heading, bullets, table, or prose.
