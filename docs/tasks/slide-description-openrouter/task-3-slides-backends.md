# Task 3 — Slides backends (agy + openrouter) + slide-input helper

**Status:** [ ]

**Spec:** `docs/specs/slide-description-openrouter.md`
**Dependencies:** Task 1 (config), Task 2 (interfaces + OpenRouter helper + error types)

## Target
`transcriber/backends/slides.py` (or `slides_index.py` for the helper) and
`tests/test_slides.py` (+ shared assertions in `tests/test_backends.py`).

## Change
- `build_slide_inputs(recording_dir, name) -> list[SlideInput]`: list
  `extracted_slides.<name>/*.jpg` in deterministic order and join each to its
  `(start_ts, end_ts)` by parsing the scenes CSV. **Scenes-CSV lives in the
  slides dir** (`extracted_slides.<name>/<name>.scenes.csv`), NOT beside the
  `.mp4` — read it there. Missing/unparseable timing → `unknown` (log a warning
  so the degraded path is visible). Zero images → `[]`.
- `OpenRouterSlidesBackend.describe(...)`: single vision chat-completions call via
  the Task 2 helper. Build one user message = leading text part (slide_extractor
  rules + language + transcript context), then per slide a `Timestamp: MM:SS -
  MM:SS` text part + `image_url` part (`data:image/jpeg;base64,...`).
  **Downscale** each JPEG to a bounded long edge (module constant, e.g. 1024 px)
  before base64. Enforce a **deterministic ceiling** (max slides and/or max total
  bytes as module constants); exceeding it raises `SlideDescribeError` **before**
  sending. Empty slide list → `""` with no HTTP call. Valid empty/whitespace
  response → `""` (not an error). Over-ceiling long decks **hard-fail** (no
  chunking/fallback).
- `AgySlidesBackend.describe(...)`: a scoped `agy` run that writes/returns the
  slide markdown only (reuse the existing prompt-building seam for the slides
  sub-task).

## Constraints
- **Do not** call any backend from `pipeline.process_recording` — the pipeline
  stays ffmpeg + scenedetect + transcribe only.
- Both backends produce the same artifact semantics; empty → `""` (Spec R9).
- Optionally pass scenedetect an absolute `-f` path in `pipeline._extract_slides`
  to pin the CSV location (coordinate with Task 7 cleanup path fix).

## Ownership
Owns: the slides backend module + helper and their tests. May make the minimal
`pipeline._extract_slides` `-f`-path edit; if so, note it for Task 7.

## Observable Acceptance
- **Tests (pytest):**
  - `build_slide_inputs`: reads CSV from the slides dir; stable order; `unknown`
    on miss (with warning); `[]` on zero images.
  - OpenRouter backend request shape: one `image_url` per slide, a `Timestamp:`
    part before each; empty images → no HTTP call, `""`; over-ceiling →
    `SlideDescribeError` before any HTTP call.
  - Agy backend (mock `run_agent`): writes/returns the slide markdown.
  - `pipeline.process_recording` makes no slides-backend call.
- **Demo:** run each backend (mocked) over a fixture slides dir (CSV in the
  slides dir) → slide markdown produced.
