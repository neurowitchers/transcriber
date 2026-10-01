# Technical Brainstorm: Replace ElevenLabs transcription with OpenRouter STT

> Status: **Phase 2 — awaiting user input.** Approaches, SWOT, and the final
> recommendation (Phases 3–4) are filled in only after the `USER_INPUT` blocks
> below are answered.

## Problem Statement & Scope

- **Core Objective:** Evaluate removing **ElevenLabs** as a transcription
  dependency by routing the `transcribe` stage through **OpenRouter**, which is
  already a configured (optional) dependency for slides and the `agno`
  summarize backend. The premise: if OpenRouter is already wired for egress,
  a second speech-to-text (STT) vendor/account/CLI is redundant.
- **Scope Boundaries:**
  - **IN:** the `transcribe` stage (`transcriber/pipeline.py::_transcribe`),
    the `transcribe.*` / `openrouter.*` config surface, prerequisites &
    privacy docs, `--dry-run`/`check` plan labels, and the ElevenLabs CLI
    prerequisite.
  - **OUT:** audio extraction (ffmpeg), scene/slide extraction, the slides
    `openrouter` vision backend, summarize backends (`agy`/`agno`), Notion,
    Telegram, and S3. These are untouched.
- **Key Constraints:**
  - **No silent cross-backend fallback** — a selected backend's failure fails
    that stage (retryable). Any new STT backend must honor this.
  - **Config references secrets by env-var NAME only**; values resolved at use
    time and never logged (including audio bytes).
  - **Idempotent, manifest-gated** stages: a retry must never re-issue a
    completed paid call. A chunked STT call must be idempotent per recording.
  - **Windows-first**, `pathlib.Path` everywhere; child processes wrapped with
    per-stage timeouts.
  - **OpenRouter STT 60-second upstream processing timeout** (see research) —
    the single hardest constraint for *meeting-length* audio.

## Technical Baseline & External Research

### Current Architecture (local audit)

- **ElevenLabs is NOT a Python package.** It is **not** in
  `pyproject.toml` `dependencies` (which lists `duct`, `agy-headless-bridge`,
  `scenedetect`, `av`, `PyYAML`, `httpx`). It is an **external CLI binary**
  invoked via `duct` in `transcriber/pipeline.py::_transcribe`:
  ```
  elevenlabs speech-to-text convert --file <mp3> --model-id <model_id> --format json
  ```
  stdout is redirected to `<name>.txt`, then the JSON is post-processed in place
  to extract the `text` field into clean plain text. So "removing the
  dependency" means removing a **required external CLI + ElevenLabs account/API
  key**, not a pip uninstall.
- **Config:** `transcribe.model_id` (e.g. `scribe_v1`) and `timeouts.elevenlabs`
  are the only transcription knobs. `transcribe` is a required config section.
- **OpenRouter is already a first-class, optional integration:**
  - `transcriber/backends/openrouter.py` holds `_openrouter_vision`, a clean,
    well-tested HTTP seam using **`httpx`** (already a core dep) against
    `POST {base_url}/chat/completions` with:
    - Bearer auth via `resolve_env(config.openrouter.api_key_env)`,
    - OpenRouter attribution headers (`HTTP-Referer`, `X-Title`),
    - bounded exponential backoff on `429`/`5xx` (`MAX_ATTEMPTS=4`),
    - strict log hygiene (no key/headers/image bytes; 200-char body snippets),
    - a `SlideDescribeError` failure contract.
  - `config.openrouter` already carries `api_key_env`, `base_url`
    (`https://openrouter.ai/api/v1`), `slides_model`, `summary_model`,
    `max_slides`. **This endpoint pattern is 90% of a reusable STT seam.**
- **Audio artifact available:** the pipeline already produces `<name>.mp3`
  (ffmpeg `libmp3lame -q:a 2`) before transcription — a compressed format
  OpenRouter STT accepts.
- **Backend-selection precedent exists:** `SUMMARY_BACKENDS = ("agy","agno")`
  and `SLIDES_BACKENDS = ("openrouter",)` with per-stage validation and
  `--dry-run`/`check` plan labels (e.g. `transcribe`, `describe-slides
  [openrouter]`, `summarize+notion [agno]`). A `transcribe.backend` selector
  would mirror this exactly.

### State of the Art / Industry Standards (OpenRouter STT — verified via docs)

OpenRouter **does** support speech-to-text, which makes the user's premise
technically viable:

- **Dedicated endpoint:** `POST /api/v1/audio/transcriptions` (same base URL,
  **same Bearer API key** as chat/vision). Returns JSON `{ text, usage }`;
  no polling, no job id. `usage` reports `seconds`, token counts, and `cost`.
- **Request contract (JSON path):**
  ```json
  {
    "model": "openai/whisper-large-v3",
    "input_audio": { "data": "<RAW base64, NOT a data: URI>", "format": "mp3" },
    "language": "en"              // optional; auto-detected if omitted
  }
  ```
  `format` is required, one of `wav|mp3|flac|m4a|ogg|webm|aac`. Also accepts
  OpenAI-style `multipart/form-data` (`file`+`model`) **capped at 25 MB**;
  files >25 MB must use the base64 JSON path.
- **Models:** `openai/whisper-1`, `openai/whisper-large-v3`,
  `openai/whisper-large-v3-turbo` (duration-priced), plus newer token-priced
  STT models (e.g. `openai/gpt-transcribe`, MAI-Transcribe 2). **STT model IDs
  do NOT appear in the default `/api/v1/models` catalog** — discover via
  `?output_modalities=transcription`.
- **CRITICAL LIMIT — 60-second upstream *processing* timeout.** This is a cap
  on processing time, not a fixed audio-length cap, but **large/long recordings
  are exactly what time out.** OpenRouter's own guidance: *"Split long audio
  into segments, transcribe each, and stitch the text."* **Meeting recordings
  (the engine's entire purpose) will routinely exceed this** → a naive 1-call
  port will fail on real inputs.
- **Error surface:** `400` (bad params / rejected `text`/`srt`/`vtt` formats),
  `401/402/403`, `413` (payload too large), `429`, `5xx`, `524` (timeout).
  `429/5xx` map cleanly onto the existing backoff policy. Zero-Completion
  Insurance: a failed transcription isn't billed.
- **Pricing:** no OpenRouter markup; `usage.cost` returns the exact per-request
  dollar cost. Whisper = per-second-of-audio; newer models = per-token.

### Relevant Ecosystem Options

- **Reuse `httpx` + the existing OpenRouter helper shape** — no new package.
- **Chunking** can be done with the **ffmpeg already on PATH** (segment the mp3
  by time, e.g. `-f segment -segment_time`), so no new dependency for splitting
  either. (This is the pivotal design decision — see Open Questions.)

## Open Questions & User Clarifications

#### Q1: Keep ElevenLabs as a selectable backend, or remove it outright?
- **Context:** The project convention is multi-backend with **no silent
  fallback** (`agy`/`agno`). We can (A) add `transcribe.backend:
  elevenlabs|openrouter` and default to one, preserving the other; or (B)
  **remove ElevenLabs entirely** and make OpenRouter the sole transcriber. (B)
  fully delivers "ElevenLabs is superfluous" but is a breaking change and loses
  the only non-OpenRouter transcription path (relevant for the privacy posture
  — see Q4). ElevenLabs `scribe_v1` is also a purpose-built diarizing STT model;
  Whisper-class quality/diarization differs.
    <!-- USER_INPUT_START:Q1 -->
    [REPLACE: "add backend selector, default openrouter, keep elevenlabs" OR "remove elevenlabs outright (breaking)" — and which default]
    <!-- USER_INPUT_END:Q1 -->

#### Q2: How should long (meeting-length) audio be handled given the 60s cap?
- **Context:** This is the make-or-break constraint. Options: (a) **ffmpeg
  time-chunking** of the mp3 into <~X-min segments, N STT calls, stitch the
  `text` in order (robust, more code, idempotency/retry design needed); (b)
  rely on `multipart` ≤25 MB single-shot and simply **fail** recordings that
  are too long/large (simplest, but fails on the primary use case); (c) pick a
  faster model (`whisper-large-v3-turbo`) and hope typical recordings fit (not
  reliable). Chunking (a) is the only option that reliably serves meeting-length
  audio.
    <!-- USER_INPUT_START:Q2 -->
    [REPLACE: preferred strategy (a/b/c). If (a): target segment length, and whether word/segment timestamps across chunk seams matter]
    <!-- USER_INPUT_END:Q2 -->

#### Q3: Which STT model should be the default, and is diarization required?
- **Context:** `transcribe.model_id` currently holds an ElevenLabs id
  (`scribe_v1`). For OpenRouter we need an OpenRouter slug. Candidates:
  `openai/whisper-large-v3-turbo` (fast, duration-priced), `openai/whisper-
  large-v3` (accuracy), or a token-priced newer model. Diarization
  (speaker labels) requires `diarize:true` + `response_format:"verbose_json"`
  and only some providers support it — does the summarize stage need speaker
  attribution, or is a flat transcript sufficient (today's `.txt` is flat)?
    <!-- USER_INPUT_START:Q3 -->
    [REPLACE: default model slug; diarization required yes/no]
    <!-- USER_INPUT_END:Q3 -->

#### Q4: Does routing transcription through OpenRouter change the privacy posture?
- **Context:** Today a **slides-off + `agy`-summarize** config performs **zero
  OpenRouter/Agno egress** — the README markets this as the sensitive-content
  path, and transcription stays with ElevenLabs. If OpenRouter becomes the
  *only* transcriber, **every** run egresses audio to OpenRouter + its routed
  upstream providers, eliminating the "no-OpenRouter" posture. Is that
  acceptable, or must a zero-OpenRouter transcription path survive (argues for
  Q1 option A)?
    <!-- USER_INPUT_START:Q4 -->
    [REPLACE: is an all-OpenRouter egress posture acceptable? must a no-OpenRouter transcription path remain?]
    <!-- USER_INPUT_END:Q4 -->

#### Q5: Should `openrouter` become required (not optional) if it's the transcriber?
- **Context:** `openrouter` config is currently required *iff* slides-enabled or
  `agno`. If OpenRouter transcribes, the `openrouter` block + API key become
  required for **every** run (even slides-off, `agy`-summarize), and
  `transcribe.model_id` semantics change from ElevenLabs id → OpenRouter slug.
  This reshapes config validation and host-migration docs.
    <!-- USER_INPUT_START:Q5 -->
    [REPLACE: confirm openrouter-required-always is acceptable, or should transcribe keep its own openrouter-less config? any backward-compat window needed?]
    <!-- USER_INPUT_END:Q5 -->

### Baseline Assumptions (used if the above are left at defaults)

- **A1 (Q1):** Add a `transcribe.backend` selector (`elevenlabs` | `openrouter`)
  rather than deleting ElevenLabs, matching the existing no-silent-fallback
  multi-backend convention. Default stays `elevenlabs` to avoid a breaking
  change until the operator opts in. *(The user's stated intent leans toward
  removal; Q1 exists to confirm the stronger, breaking variant.)*
- **A2 (Q2):** Meeting-length audio **requires ffmpeg time-chunking** + ordered
  text stitching; a single-shot port is treated as insufficient.
- **A3 (Q3):** Default `openai/whisper-large-v3-turbo`, flat transcript (no
  diarization) to match today's flat `.txt`.
- **A4 (Q4):** The zero-OpenRouter path must survive → reinforces A1 (keep
  ElevenLabs selectable).
- **A5 (Q5):** When `transcribe.backend == "openrouter"`, the `openrouter`
  block + its API-key env var become required; otherwise unchanged.

## Architectural Approaches Evaluated

_(Phase 3 — completed after USER_INPUT above is filled.)_

## Structured Comparison & Methodology

_(Phase 3 — SWOT matrix completed after USER_INPUT.)_

## Recommendation

_(Phase 4 — completed after USER_INPUT.)_
