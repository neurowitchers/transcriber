# Technical Brainstorm: Replace ElevenLabs transcription with OpenRouter STT

> Status: **Complete (Phases 1–4).** Decisions resolved from USER_INPUT;
> approaches, SWOT, recommendation, and a phased execution plan are below.

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
    Fully replace - it is not cost-effective
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
    (a)
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
    Accurary matter, diarization is required
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
    No, I don't care
    <!-- USER_INPUT_END:Q4 -->

#### Q5: Should `openrouter` become required (not optional) if it's the transcriber?
- **Context:** `openrouter` config is currently required *iff* slides-enabled or
  `agno`. If OpenRouter transcribes, the `openrouter` block + API key become
  required for **every** run (even slides-off, `agy`-summarize), and
  `transcribe.model_id` semantics change from ElevenLabs id → OpenRouter slug.
  This reshapes config validation and host-migration docs.
    <!-- USER_INPUT_START:Q5 -->
    Require it
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

## Resolved Decisions (from USER_INPUT)

| Q | Decision |
| --- | --- |
| Q1 | **Fully replace** ElevenLabs — not cost-effective. No `elevenlabs` backend retained. |
| Q2 | **(a) ffmpeg time-chunking** + ordered stitch for meeting-length audio. |
| Q3 | **Accuracy matters, diarization REQUIRED.** → rules out plain Whisper. |
| Q4 | All-OpenRouter egress is acceptable; no zero-OpenRouter path needed. |
| Q5 | **`openrouter` config becomes required** for every run. |

### Research update that reshapes the design (Q3 diarization)

Diarization on OpenRouter is **model/provider-specific**, surfaced via
`response_format: "verbose_json"` plus a provider-specific toggle — and **plain
Whisper (`whisper-large-v3` / `-turbo`) does NOT diarize.** So the obvious
"default to Whisper turbo" choice is *invalid* under Q3. Diarizing options on
OpenRouter today:

| Model | Diarization mechanism | Accuracy / notes | Price |
| --- | --- | --- | --- |
| **Microsoft MAI-Transcribe 2** | `provider.options.azure.diarization.enabled: true` + `verbose_json` | **#1 FLEURS multilingual**; 60 langs, code-switching, word timestamps, keyword biasing | $0.10/hr |
| Google Gemini 3.5 Transcribe | native diarization, ≤8 speakers | **≤30 min/clip when diarization or timestamps on** (chunk-friendly), token-priced | $2/$12 per M tok |
| Deepgram Nova-3 | Deepgram diarization | strong production STT | ~$0.000072/s |
| Grok STT 1.0 | optional diarization + word timestamps | multichannel | $0.000028/s |
| Fish Audio Transcribe 1 Pro | inline `speaker` markers | tuned for meetings/podcasts/interviews | $0.0001/s |

Implications baked into the approaches below:
- The default model must be a **diarizing** one, not Whisper. Candidate default:
  **`microsoft/mai-transcribe-2`** (top accuracy + explicit diarization +
  keyword biasing + cheap per-hour pricing).
- Diarization ⇒ **`response_format: "verbose_json"`**; we parse `segments`
  (and/or `words` with `speaker`/`speaker_label`) and render speaker-attributed
  text, not just the flat `text` field.
- Chunking (Q2) now must **reconcile speaker labels across chunk seams** —
  provider diarization numbers speakers per-request, so chunk 2's "Speaker 1"
  may be chunk 1's "Speaker 2". This is the central new risk (see Risks).
- Diarization is provider-gated: a non-diarizing model/provider returns **400**.
  The backend must fail cleanly (no silent fallback) with an actionable message.

## Architectural Approaches Evaluated

All three satisfy Q1 (remove ElevenLabs), Q4 (all-OpenRouter), Q5 (require
`openrouter`). They differ in **how diarized, meeting-length transcription is
produced** and how much new machinery is introduced.

### Approach A: Minimal diarized port — single-shot `verbose_json`, no chunking
- **Concept:** Replace `_transcribe` with one `POST /audio/transcriptions`
  call (base64 mp3, `response_format:"verbose_json"`, diarization toggle),
  reuse the existing `httpx`+backoff helper shape, render speaker-attributed
  text from `segments` into `<name>.txt`.
- **Component Changes:** `pipeline.py::_transcribe`; new
  `backends/transcribe_openrouter.py` (or extend `backends/openrouter.py` with
  an `_openrouter_transcribe` seam); config: `transcribe.model_id` → OpenRouter
  slug + diarization/verbose settings; drop the `elevenlabs` CLI + prereq.
- **Dependencies Introduced:** None (reuses `httpx`, ffmpeg already present).
- **Fatal flaw:** ignores Q2 — **fails on real meeting audio** past the ~60 s
  processing timeout / 25 MB cap. Rejected on arrival, kept only as the
  "what-not-to-do" baseline.

### Approach B (RECOMMENDED): Chunked diarized STT with seam reconciliation
- **Concept:** A dedicated transcribe backend that:
  1. **Segments** the existing `<name>.mp3` with the ffmpeg already on PATH
     (`-f segment -segment_time <N>` with a small overlap, deterministic
     `part_000.mp3`… names under a per-recording work dir).
  2. Transcribes each segment via `POST /audio/transcriptions`
     (`verbose_json` + diarization), reusing the proven retry/backoff/
     log-hygiene policy from `backends/openrouter.py`.
  3. **Stitches** segment transcripts in index order, offsetting timestamps by
     cumulative chunk start, and **reconciles speaker labels across seams**
     (overlap-window voice-embedding/label-matching, or a documented best-effort
     heuristic — see Risks R-seam).
  4. Writes a speaker-attributed `<name>.txt` (same artifact contract the
     summarize stage already consumes).
  - **Idempotency:** a manifest/sidecar records completed segments so a retry
    re-issues only the *failed* chunk's paid call (mirrors the existing
    manifest-gated, no-re-issue rule). Segment files cleaned up with the other
    intermediates unless `--keep-intermediates`.
- **Component Changes:** new `backends/transcribe_openrouter.py` +
  `_openrouter_transcribe` seam; `pipeline.py` transcribe stage delegates to it;
  config `transcribe`: `model_id` (default `microsoft/mai-transcribe-2`),
  `diarize: true`, `segment_seconds`, plus `timeouts` rename
  `elevenlabs` → `transcribe` (keep `elevenlabs` as a silently-ignored legacy
  key or hard-error per migration policy); `openrouter` required; `--dry-run`/
  `check` plan label `transcribe [openrouter:<model>]`; prereqs/privacy docs
  rewritten; delete the ElevenLabs CLI prerequisite + `scribe_v1` references.
- **Dependencies Introduced:** None new (ffmpeg + `httpx` already present).

### Approach C: Chunked, but defer diarization to a per-chunk LLM pass
- **Concept:** Same chunking as B, but transcribe with a cheap non-diarizing
  model and run a **second** OpenRouter *chat* pass that labels speakers from
  the raw transcript (and optionally the slides context).
- **Component Changes:** B's chunker + a new chat-completions "diarize/label"
  prompt + merge step.
- **Dependencies Introduced:** None, but **doubles the OpenRouter calls** and
  degrades diarization quality (text-only speaker inference is weaker than
  acoustic diarization). Contradicts "accuracy matters."

## Structured Comparison & Methodology

### SWOT Matrix

| Approach | Strengths | Weaknesses | Opportunities | Threats/Risks |
| --- | --- | --- | --- | --- |
| **A: single-shot** | Smallest diff; reuses helper 1:1; trivial to test | **Fails on meeting-length audio** (60 s / 25 MB); violates Q2 | Fine for ≤~1 min voice memos only | Guaranteed production failure on the core use case |
| **B: chunked + diarized (REC)** | Serves real meetings; reuses ffmpeg+`httpx`; honors no-silent-fallback & manifest idempotency; top-accuracy diarizing default | Most code (chunker, stitcher, **seam label reconciliation**); more tests | Reusable chunk/stitch utility; `usage.cost` metering; keyterm biasing for jargon | Cross-chunk speaker-label drift; provider diarization availability/format drift; per-request cost on long audio |
| **C: chunk + LLM-labeled** | Model-agnostic STT; cheap STT model | 2× calls; **weaker diarization**; extra prompt to maintain | Could enrich labels with slide/context | Text-only speaker inference inaccurate — fights Q3 "accuracy matters" |

## Recommendation

**Adopt Approach B — a chunked, diarized OpenRouter STT backend that fully
replaces ElevenLabs.** It is the only option that simultaneously satisfies every
confirmed decision: it removes ElevenLabs (Q1), chunks meeting-length audio
(Q2), uses an acoustic-diarizing, top-accuracy model (Q3), accepts all-
OpenRouter egress (Q4), and makes `openrouter` required (Q5). It reuses the two
assets the engine already has — **ffmpeg** for segmentation and the hardened
**`httpx` + backoff + log-hygiene** OpenRouter seam — so it introduces **zero
new dependencies** while deleting one external CLI + one vendor account.

Approach A is retained only as the documented anti-pattern (it fails on the
primary use case). Approach C is rejected because text-only speaker labeling
undercuts the explicit "accuracy matters + diarization required" bar.

**Default model: `microsoft/mai-transcribe-2`** — #1 on FLEURS multilingual,
explicit speaker diarization, word-level timestamps, keyword biasing for
meeting jargon, and inexpensive per-hour pricing. `transcribe.model_id` is
operator-overridable to any diarization-capable slug (Gemini 3.5 Transcribe,
Deepgram Nova-3, Grok STT, Fish Audio Transcribe 1 Pro).

### Key Risks & Mitigations

| Risk | Mitigation |
| --- | --- |
| **R-seam: cross-chunk speaker-label drift** (provider renumbers speakers per request) | Add a small **overlap window** between chunks and match labels on the overlap (word/segment alignment); if unavailable, document the limitation and expose labels as `Speaker A/B…` per global reconciliation pass. Make seam strategy unit-testable with synthetic multi-chunk fixtures. |
| **60 s processing timeout / 25 MB cap** | Deterministic `segment_seconds` default sized well under the cap (e.g. 5–8 min of compressed mp3); validate each segment's byte size; prefer base64 JSON path for >25 MB safety. |
| **Diarization provider-gated → 400** | Validate the configured model against a known diarizing set at config time where possible; on 400 raise an actionable `TranscribeError` (no silent fallback), surfaced in `check`. |
| **Per-request cost on long audio** | Read `usage.cost` per chunk, log aggregate (never log audio bytes); document cost model in README privacy/cost section. |
| **Retry re-issues paid chunk calls** | Per-segment manifest/sidecar: completed chunk text persisted; retry re-issues only failed chunks (mirrors existing manifest-gating). |
| **`verbose_json` shape varies by provider** | Centralize response parsing in one seam with defensive extraction (like `_extract_content`); tolerate missing `words`, fall back to `segments`, then flat `text` with a logged warning. |
| **Breaking config migration** | Rewrite README "Host maintainers — config migration" note: ElevenLabs removed; `transcribe.model_id` now an OpenRouter slug; `openrouter` now always required; `timeouts.elevenlabs` → `timeouts.transcribe`. |

### Phased Execution Plan (ready for `Seed` → full spec → `Split Tasks`)

1. **Config reshape** — `transcribe`: `model_id` default `microsoft/mai-
   transcribe-2`, add `diarize`, `segment_seconds`; make `openrouter` required
   unconditionally; `timeouts.elevenlabs` → `timeouts.transcribe` (migration
   handling). Update validation + `test_config.py`.
2. **OpenRouter transcribe seam** — `_openrouter_transcribe` in
   `backends/openrouter.py` (or a sibling): `POST /audio/transcriptions`,
   base64 mp3, `verbose_json` + diarization, reuse backoff/log-hygiene; parse
   `segments`/`words` → speaker-attributed text. Unit tests with mocked `httpx`.
3. **Chunker + stitcher** — ffmpeg segmentation of `<name>.mp3`, ordered
   stitch, timestamp offsetting, **seam speaker-label reconciliation**;
   per-segment idempotency manifest. Tests with synthetic multi-chunk fixtures.
4. **Pipeline wiring** — `pipeline.py` transcribe stage delegates to the new
   backend; remove `elevenlabs` CLI invocation; keep artifact contract
   (`<name>.txt`). Update `test_pipeline.py`.
5. **CLI surface** — `--dry-run`/`check` label `transcribe [openrouter:<model>]`;
   `check` validates `openrouter` presence + diarization-capable model.
6. **Docs & examples** — rewrite README pipeline/prereqs/privacy/migration;
   update both `examples/*.config.yaml` (drop `scribe_v1`, add OpenRouter STT
   block); remove ElevenLabs prerequisite everywhere.
7. **Cleanup** — delete dead ElevenLabs code paths/tests; ensure segment temp
   files are cleaned unless `--keep-intermediates`.

### Summary Table

| Decision | Choice | Rationale |
| --- | --- | --- |
| Replace vs. keep ElevenLabs | **Replace entirely** | Q1: not cost-effective; OpenRouter already configured. |
| Long-audio handling | **ffmpeg chunk + stitch** | Q2 + 60 s/25 MB limit; no new dependency (ffmpeg present). |
| Default STT model | **`microsoft/mai-transcribe-2`** | Q3: top FLEURS accuracy **and** acoustic diarization + keyword biasing. |
| Diarization transport | **`verbose_json`** + provider toggle | Required for speaker labels; plain Whisper can't diarize. |
| Egress posture | **All-OpenRouter accepted** | Q4: no zero-OpenRouter path to preserve. |
| `openrouter` config | **Required always** | Q5: it is now the transcriber. |
| HTTP seam | **Reuse `httpx` + backoff/log-hygiene** | Proven, no-silent-fallback, no new deps. |
| Idempotency | **Per-segment manifest** | Retry re-issues only failed chunk's paid call. |
| Approach | **B (chunked + diarized)** | Only option satisfying all five decisions. |
