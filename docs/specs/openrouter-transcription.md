# Spec: Replace ElevenLabs transcription with OpenRouter diarized STT

> **Critique pass 1 applied (2026-10-01).** Incorporated all findings from
> `docs/critiques/openrouter-transcription.md`: **E1/X1** — chunking now uses
> explicit time-sliced `ffmpeg -ss/-t` calls (the `segment` muxer has no overlap
> option); **P1** — `transcribe.diarize` is the single switch (true → verbose_json
> + provider toggle; false → omit toggle, flat text); **E2** — work dir
> standardized to `recordings_dir/transcribe_work.{name}`; plus **P2** (no-timestamp
> `Speaker <ID>:` lines), **P3** (JSON base64 transport, not multipart), **E3**
> (drop `elevenlabs` from `_ALWAYS_BINARIES`), **E4** (global speaker registry +
> unmatched-speaker fallback), **E5** (manifest records + guards
> `segment_seconds`/`overlap_seconds`). Verdict was ⚠️ PROCEED WITH UPDATES.

## Problem Statement

The engine's `transcribe` stage shells out to an external **ElevenLabs** CLI
(`elevenlabs speech-to-text convert`), requiring a second vendor account, a
second API key, and a required `elevenlabs` binary on `PATH`. OpenRouter is
**already** a first-class (optional) integration — the slides vision backend and
the `agno` summarize backend both call it through a hardened `httpx` seam. The
second STT vendor is redundant and not cost-effective.

**Replace ElevenLabs entirely** with OpenRouter's speech-to-text endpoint
(`POST /api/v1/audio/transcriptions`), reusing the existing OpenRouter base
URL + API key + HTTP seam. Because meeting recordings routinely exceed
OpenRouter's **~60 s upstream processing timeout** (and the **25 MB per-call
cap**), the stage must **chunk the audio, transcribe each chunk, and stitch the
results** — producing a **speaker-attributed (diarized)** `<name>.txt`
transcript, since accuracy and diarization are required.

This is a **breaking change**: `transcribe.model_id` changes semantics
(ElevenLabs id → OpenRouter STT slug), `openrouter` config becomes **always
required**, and the `elevenlabs` CLI prerequisite is removed.

## Requirements

### Transcription backend

- **R1.** The `transcribe` stage calls `POST {openrouter.base_url}/audio/
  transcriptions` instead of the `elevenlabs` CLI. The ElevenLabs CLI invocation
  and its prerequisite are **removed** entirely (no `elevenlabs` backend is
  retained).
- **R2.** The stage produces the same artifact contract: a plain-text
  `<name>.txt` that the summarize stage consumes. The text is **speaker-
  attributed** (diarized) when the model returns speaker labels, rendered as
  `Speaker <ID>: <text>` lines (one block per speaker turn) **with no timestamp
  markers** — preserving the existing flat-text contract the summarize stage
  expects. When no speaker labels are available (see R5/R14), the file is flat
  text with no `Speaker <ID>:` prefixes. *(P2)*
- **R3.** The API key is resolved at use time from `openrouter.api_key_env` via
  `resolve_env`; it is **never logged**. Audio bytes, request headers, and the
  base64 payload are **never logged** (log hygiene, matching the slides seam).

### Diarization & accuracy

- **R4.** Diarization is **controlled by `transcribe.diarize`** (default `true`;
  see R14), and the engine's default configuration **requires** it.
  - When `diarize: true` (default): the request sends `response_format:
    "verbose_json"` **plus** the provider-specific diarization toggle appropriate
    to the configured model, and output is speaker-attributed (R2). The default
    model is a diarization-capable, high-accuracy slug (R6).
  - When `diarize: false` (operator opt-out, e.g. targeting a non-diarizing
    model): the provider diarization toggle is **omitted** from the request body;
    the parser outputs **flat text with no `Speaker <ID>:` prefixes** (R2). This
    resolves the R4/R14 relationship: `diarize` is the single switch; there is no
    hidden second requirement. *(P1)*
- **R5.** The response parser reads speaker-labeled `segments`/`words` and
  renders speaker-attributed text. It degrades defensively: missing `words` →
  use `segments`; missing both → flat `text` with a **logged warning** (never
  a silent crash).
- **R6.** The **default `transcribe.model_id` is `microsoft/mai-transcribe-2`**
  (FLEURS #1 multilingual, explicit diarization, word timestamps, keyword
  biasing). Operators may override it with any diarization-capable OpenRouter
  slug. Plain Whisper (`whisper-large-v3`/`-turbo`) is **not** a valid default
  because it cannot diarize.

### Chunking & stitching (meeting-length audio)

- **R7.** The stage **segments `<name>.mp3`** using the `ffmpeg` already on
  `PATH` (no new dependency) into deterministic, ordered, **overlapping** parts.
  Because the `ffmpeg` `segment` muxer does **not** support overlap, each part is
  cut with an **explicit time-sliced extraction** — one ffmpeg call per part:
  `ffmpeg -ss <start> -t <segment_seconds + overlap_seconds> -i <mp3> -c copy
  <part_path>` (so part *k* starts at `k * segment_seconds` and runs
  `segment_seconds + overlap_seconds`, giving an `overlap_seconds` tail shared
  with part *k+1*). Parts are named `part_000.mp3`, `part_001.mp3`, … under the
  per-recording work directory **`recordings_dir / f"transcribe_work.{name}"`**
  (mirroring the existing `extracted_slides.{name}` convention — E2). Segment
  length is sized well under the 60 s / 25 MB limits
  (`transcribe.segment_seconds`, default `480`); the overlap window
  (`transcribe.overlap_seconds`, default `5`) exists to support seam
  reconciliation (R9). *(E1 / E2 / X1)*
- **R8.** Each segment is transcribed with a single `/audio/transcriptions`
  call. Results are **stitched in index order**; segment timestamps are offset
  by the cumulative chunk start (minus overlap) so the merged transcript has a
  monotonic timeline.
- **R9.** **Cross-chunk speaker labels are reconciled against a global speaker
  registry.** Provider labels are per-request, so chunk N+1's local ids are
  mapped onto a **persistent global registry** (`Speaker 0`, `Speaker 1`, …
  spanning the whole recording) using the overlap window (match speakers whose
  overlapping-region text/time aligns with the tail of chunk N). The
  reconciliation strategy is a documented, unit-testable heuristic.
  **Unmatched-speaker fallback (E4):** when a speaker is silent during the
  overlap window (so their chunk N+1 local id cannot be matched), they are
  assigned a **new** global id rather than dropped or force-merged, and the
  ambiguity is **logged**. This can over-count speakers on long recordings; that
  is the accepted, documented trade-off (prefer distinct-but-safe over
  wrong-merge). Labels are never dropped.

### Idempotency, failure, limits

- **R10.** **No silent fallback** (engine-wide rule). A configured model that
  cannot diarize returns `400`; the stage raises an actionable `TranscribeError`
  and fails (retryable). Transport/`429`/`5xx` errors reuse the existing bounded
  exponential backoff.
- **R11.** **Per-segment idempotency.** A manifest/sidecar
  (`transcribe_work.{name}/segments.json`) records completed segments and their
  transcripts; a retry re-issues the `/audio/transcriptions` call **only for
  failed/missing segments**, never re-paying for completed ones (mirrors the
  engine's manifest-gated, no-re-issue-of-completed-paid-calls rule). The overall
  stage remains idempotent: an existing non-empty `<name>.txt` is skipped.
  **Parameter guard (E5):** the manifest records the active `segment_seconds` and
  `overlap_seconds`; if either differs from the current config on a retry, the
  cached segments are **invalidated** (the work dir is rebuilt from scratch) so
  mismatched chunk boundaries can never corrupt the stitched timeline.
- **R12.** Per-run cost is observable: aggregate `usage.cost` across segments is
  logged (dollars only; no audio content). The stage honors a per-stage timeout
  (`timeouts.transcribe`, see R15).

### Config & CLI

- **R13.** `openrouter` config becomes **unconditionally required** (it is now
  the transcriber), in addition to the existing slides/`agno` triggers.
- **R14.** `transcribe` config gains `diarize: bool` (default `true`),
  `segment_seconds: int` (default `480`), `overlap_seconds: int` (default `5`);
  `model_id` default becomes `microsoft/mai-transcribe-2`. `transcribe.model_id`
  semantics change to an OpenRouter STT slug.
- **R15.** `timeouts.elevenlabs` is renamed to `timeouts.transcribe`. The legacy
  `elevenlabs` key follows the project's migration policy: it is **silently
  ignored** if present (documented), and `timeouts.transcribe` defaults to
  `DEFAULT_TIMEOUT_SECONDS` (900).
- **R16.** `--dry-run` / `check` surface the transcribe backend:
  the plan label reads `transcribe [openrouter:<model>]`. `check` validates that
  `openrouter` is configured and that `OPENROUTER_API_KEY` (env-var named by
  `openrouter.api_key_env`) is set, before any work starts. **The `elevenlabs`
  entry is removed from `_ALWAYS_BINARIES` in `transcriber/__main__.py` (E3)** so
  `check` no longer fails on hosts without the legacy CLI (`ffmpeg` remains the
  only always-required binary; `scenedetect` stays slides-gated as today).

### Docs & cleanup

- **R17.** README is updated: Pipeline (transcribe step), Prerequisites (remove
  the ElevenLabs CLI + account; `openrouter` now always required),
  Privacy/data-egress (audio egresses to OpenRouter on **every** run), schema
  table, and the "Host maintainers — config migration" note.
- **R18.** Both `examples/*.config.yaml` drop `scribe_v1`, add the OpenRouter STT
  config, and remove all ElevenLabs references.
- **R19.** Dead ElevenLabs code paths and tests are removed, **including the
  `"elevenlabs"` entry in `_ALWAYS_BINARIES`** (E3). Segment temp files and the
  per-recording work dir **`transcribe_work.{name}`** are cleaned up with the
  other intermediates unless `--keep-intermediates` (the `.txt` transcript
  remains a durable output).

## Background (codebase internals)

- **`transcriber/pipeline.py`** — the deterministic media pipeline. `_transcribe`
  currently runs `elevenlabs speech-to-text convert --file <mp3> --model-id
  <model_id> --format json`, redirects stdout to `<name>.txt`, then rewrites the
  file with the decoded `text` field. All child processes go through
  `_run_command(argv, timeout, stdout_path=…)` (the sole subprocess seam, which
  tests monkeypatch) with `PipelineTimeoutError` on timeout. `process_recording`
  runs: mp3 (ffmpeg) → optional slides (scenedetect) → transcribe. The transcribe
  branch is gated on `txt.exists()` (idempotency) and `have_mp3`.
- **`transcriber/backends/openrouter.py`** — `_openrouter_vision(config,
  messages, model, *, timeout)` is the proven HTTP seam: `httpx.post`,
  `Authorization: Bearer <resolve_env(api_key_env)>`, attribution headers,
  bounded exponential backoff (`MAX_ATTEMPTS=4`, retry `{429,500,502,503,504}`),
  `_snippet` body truncation (200 chars), strict log hygiene, and
  `_extract_content` defensive parsing. **The new STT seam mirrors this file's
  structure** (`_openrouter_transcribe`) but posts to `/audio/transcriptions`
  with an `input_audio` body and parses the STT response shape.
- **`transcriber/config.py`** — dataclass model + validator. `Transcribe` has a
  single field `model_id`. `OpenRouter` is optional and required *iff* slides
  enabled or `summary.backend == "agno"` (the `needs_openrouter` check). `Timeouts`
  has an `elevenlabs` field. `resolve_env(name)` resolves secrets at use time and
  raises `MissingEnvVarError`. `SLIDES_BACKENDS`/`SUMMARY_BACKENDS` tuples +
  per-field validation are the precedent for any new constrained value.
- **`transcriber/backends/errors.py`** — `SlideDescribeError`, `SummarizeError`.
  Add a sibling `TranscribeError` here (same log-hygiene contract).
- **`transcriber/backends/summarize_agno.py`** — reference for reusing the
  OpenRouter connection fields (`api_key_env`, `base_url`, model id) and
  resolving secrets at use time.
- **`transcriber/cleanup.py`** — `INTERMEDIATE_SUFFIXES`/`KEEP_SUFFIXES`;
  `.txt` is a **KEEP** suffix (durable transcript) and must stay kept. The new
  per-recording segment work dir + `.part_*.mp3` files must become cleanup
  candidates (like `extracted_slides.<name>/`).
- **`transcriber/__main__.py`** — orchestrator; builds the `--dry-run`/`check`
  plan and does pre-flight env/config validation before work starts.
- **Tests** — `tests/test_pipeline.py` (monkeypatches `_run_command`),
  `tests/test_config.py`, `tests/test_examples.py`, `tests/test_backends.py`,
  `tests/test_cleanup.py`, `tests/test_main.py`. New seam tests mock `httpx`
  (see `tests/test_slides.py` for the mocking pattern).

## Proposed Solution

### Data flow

```mermaid
flowchart TD
    A["&lt;name&gt;.mp4"] -->|ffmpeg| B["&lt;name&gt;.mp3"]
    B -->|per-part ffmpeg -ss START -t SEG+OVERLAP<br/>(explicit time slices, overlapping)| C["transcribe_work.&lt;name&gt;/part_000.mp3 … part_NNN.mp3"]
    C --> D{segments.json manifest:<br/>done AND params match?}
    D -->|yes| E[reuse saved segment transcript]
    D -->|no / params changed| F["POST /audio/transcriptions<br/>verbose_json + diarize<br/>(httpx + backoff, log-hygiene)"]
    F --> G[parse segments/words → speaker-labeled text]
    G --> H[persist segment transcript + mark manifest]
    E --> I[stitch in index order<br/>offset timestamps<br/>reconcile labels vs global registry across overlap]
    H --> I
    I --> J["&lt;name&gt;.txt (Speaker ID: text, no timestamps, durable)"]
    J --> K[cleanup: delete transcribe_work.&lt;name&gt;<br/>unless --keep-intermediates]
```

### Component changes

| File | Change |
| --- | --- |
| `transcriber/backends/transcribe_openrouter.py` *(new)* | `_openrouter_transcribe(config, audio_path, *, model, diarize, language, timeout) -> TranscriptChunk`; mirrors `openrouter.py` seam. Also `chunk_audio(...)`, `stitch(...)`, `reconcile_speakers(...)`, and the stage entry `transcribe_recording(mp3, txt, config)`. |
| `transcriber/backends/errors.py` | Add `TranscribeError(RuntimeError)`. |
| `transcriber/pipeline.py` | `_transcribe` delegates to `transcribe_recording`; remove the `elevenlabs` argv + JSON post-processing. `process_recording` passes `config` (not just `model_id`). |
| `transcriber/config.py` | `Transcribe` gains `diarize`, `segment_seconds`, `overlap_seconds`; `model_id` default `microsoft/mai-transcribe-2`. `OpenRouter` now **always required** (extend `needs_openrouter` → always true). `Timeouts.elevenlabs` → `transcribe`; ignore legacy `elevenlabs` key. |
| `transcriber/cleanup.py` | Add the per-recording work dir `transcribe_work.{name}/` (holding `part_*.mp3` + `segments.json`) to `intermediate_paths` as an `rmtree` candidate, like `extracted_slides.{name}/`. |
| `transcriber/__main__.py` | Plan label `transcribe [openrouter:<model>]`; pre-flight validate `openrouter` + API-key env. |
| `README.md`, `examples/*.config.yaml` | Per R17/R18. |

### Request/response contract

```jsonc
// POST {base_url}/audio/transcriptions   (Authorization: Bearer <key>)
{
  "model": "microsoft/mai-transcribe-2",
  "input_audio": { "data": "<RAW base64 mp3, NOT a data: URI>", "format": "mp3" },
  "language": "en",                 // optional; omit to auto-detect
  "response_format": "verbose_json",
  "provider": { "options": { "azure": { "diarization": { "enabled": true } } } }
}
// 200 → { "text": "...", "segments": [...], "words": [...]?, "usage": { "cost": 0.0005, "seconds": 9.2, ... } }
// 400 (model can't diarize / bad format) → TranscribeError (no fallback)
```

> The diarization toggle is model/provider-specific (e.g. the `azure.*` block for
> `mai-transcribe-2`). The seam builds the toggle from a small per-model mapping;
> an unknown model sends `verbose_json` without a provider toggle and relies on
> native diarization, failing cleanly on a 400 if unsupported.
>
> **When `transcribe.diarize == false` (P1):** omit the `provider` diarization
> block entirely; `response_format` may stay `verbose_json` for timestamps but the
> parser emits flat text (no `Speaker <ID>:` prefixes, R2).
>
> **Transport note (P3):** OpenRouter accepts this **JSON base64** body on
> `/audio/transcriptions` (it is not the OpenAI `multipart/form-data` file-upload
> path). The seam uses the JSON path deliberately (files can exceed the 25 MB
> multipart cap) and sets `Content-Type: application/json` with the same clean
> Bearer/attribution headers as the vision seam.

### Data models (sketch)

```python
@dataclass
class SpeakerSegment:
    speaker: str            # provider label, e.g. "0"/"Speaker 1"
    start: float            # seconds, chunk-local before offsetting
    end: float
    text: str

@dataclass
class TranscriptChunk:
    index: int
    offset: float           # cumulative start (seconds) in the full timeline
    segments: list[SpeakerSegment]
    cost: float             # usage.cost for this chunk (0.0 if absent)
    raw_text: str           # flat fallback

# segments.json manifest (per recording) — also carries the params guard (E5):
# { "segment_seconds": 480, "overlap_seconds": 5,
#   "parts": [ {"index": 0, "done": true, "segments": [...], "cost": 0.0005}, ... ] }
# On retry, if segment_seconds/overlap_seconds differ from config → discard + rechunk.
```

Rendering (R2/P2): map each chunk's provider labels onto a **global speaker
registry** (E4), then group consecutive same-global-speaker segments into
`Speaker <ID>: <text>` lines **with no timestamp markers**. If no speaker labels
exist anywhere (or `diarize == false`), write the stitched flat text (R5).

## Task Breakdown

### Task 1 — Config reshape (`transcribe` + mandatory `openrouter` + timeouts)
- **Objective:** Make config express OpenRouter diarized STT and require
  `openrouter` unconditionally.
- **Guidance:** In `config.py`: extend `Transcribe` with `diarize: bool = True`,
  `segment_seconds: int = 480`, `overlap_seconds: int = 5`, and change
  `model_id` default to `microsoft/mai-transcribe-2`. Rename `Timeouts.elevenlabs`
  → `transcribe` (default `DEFAULT_TIMEOUT_SECONDS`); drop any `elevenlabs` key
  from the input before building `Timeouts` (silent-ignore) so legacy configs
  load. Set `needs_openrouter = True` always (keep the existing slides/agno
  reasons in the error message, but require `openrouter` for every config).
- **Tests (`test_config.py`):** `openrouter`-absent config now raises
  `ConfigError`; `transcribe` defaults populate; a config with legacy
  `timeouts.elevenlabs` loads and the value is ignored; `segment_seconds`/
  `diarize` round-trip from YAML and JSON.
- **Demo:** `uv run python -c "from transcriber.config import load; print(load('examples/acme.config.yaml').transcribe)"`.

### Task 2 — OpenRouter STT seam (`_openrouter_transcribe`)
- **Objective:** A single HTTP seam that transcribes one audio file with
  diarization and returns a parsed `TranscriptChunk`.
- **Guidance:** New `transcriber/backends/transcribe_openrouter.py`. Copy the
  retry/backoff/`_snippet`/log-hygiene scaffolding from `openrouter.py`. POST to
  `{base_url}/audio/transcriptions` with the R-contract body as **JSON base64**
  (`Content-Type: application/json`; base64-encode the file as **raw base64, not
  a data: URI**; the JSON path, not OpenAI multipart — P3). When `diarize` is
  true, include `response_format: "verbose_json"` + the per-model diarization
  toggle from a small mapping (start with `mai-transcribe-2` → azure block); when
  `diarize` is false, **omit** the provider toggle and emit flat text (P1). Parse
  `segments`/`words`/`usage.cost` defensively into `SpeakerSegment`s; fall back
  to flat `text` with a logged warning (R5). Add `TranscribeError` to
  `errors.py`. Never log key/headers/audio.
- **Tests (`test_transcribe.py`, mock `httpx`):** 200 with `segments` → parsed
  speakers; 200 with only `text` → flat fallback + warning; `diarize=false` →
  request body carries **no** provider diarization block and output has no
  `Speaker` prefixes; `400` → `TranscribeError` (no retry); `429`→`200` → retried
  then succeeds; assert the key/base64 never appear in logs or the exception
  message.
- **Demo:** unit test run; optionally a gated live call behind an env flag.

### Task 3 — Chunker + stitcher + speaker reconciliation
- **Objective:** Turn a long mp3 into one diarized transcript via **explicit
  time-sliced** ffmpeg extraction and ordered stitching with cross-seam speaker
  mapping.
- **Guidance:** `chunk_audio(mp3, work_dir, segment_seconds, overlap_seconds,
  timeout)` → ordered part paths via the pipeline's `_run_command` ffmpeg seam,
  **one call per part**: `ffmpeg -ss <k*segment_seconds> -t <segment_seconds +
  overlap_seconds> -i <mp3> -c copy <work_dir>/part_{k:03d}.mp3` (the segment
  muxer has **no** overlap option — E1). `work_dir` is `recordings_dir /
  f"transcribe_work.{name}"` (E2). `stitch(chunks)` offsets each chunk's segment
  times by its cumulative start (`k*segment_seconds`) and concatenates.
  `reconcile_speakers(chunks)` maps each chunk's provider labels onto a **global
  speaker registry** using the overlap window (align overlapping-region segments
  by time/text); an unmatched speaker (silent in the overlap) gets a **new**
  global id and a logged warning — never force-merged or dropped (R9/E4). Render
  `Speaker <ID>: <text>` lines, no timestamps (P2).
- **Tests:** synthetic multi-chunk `TranscriptChunk` fixtures (no real audio):
  2–3 chunks where provider labels are permuted across seams → reconciliation
  yields consistent global speakers; a speaker absent from an overlap → new
  global id (asserted) + warning; timestamp offsets are monotonic;
  no-speaker-labels / `diarize=false` input → flat stitched text. `chunk_audio`
  tested with `_run_command` monkeypatched (assert **per-part `-ss`/`-t`** argv
  and the `transcribe_work.<name>` path, not a `-f segment` call).
- **Demo:** `uv run pytest tests/test_transcribe.py -q`.

### Task 4 — Pipeline wiring + per-segment idempotency
- **Objective:** Replace the ElevenLabs path; make retries re-issue only failed
  segments.
- **Guidance:** Add `transcribe_recording(mp3, txt, config)` that orchestrates
  chunk → (manifest check → seam call → persist) per segment → stitch →
  reconcile → write `<name>.txt`. Persist the per-recording manifest
  `recordings_dir / f"transcribe_work.{name}" / "segments.json"` storing
  **`segment_seconds` + `overlap_seconds`** plus per-part transcript + done flag;
  a retry skips done parts, but if the stored params differ from the current
  config it **discards the work dir and rechunks** (E5). In `pipeline.py`,
  `_transcribe` calls this; remove the elevenlabs argv and JSON post-processing;
  keep the `txt.exists()` stage-level skip. Pass `config` through
  `process_recording`.
- **Tests (`test_pipeline.py`):** monkeypatch the seam; a 2-segment recording
  writes a diarized `<name>.txt`; a simulated failure on segment 2 then rerun
  re-calls the seam **only** for segment 2 (assert call count); changing
  `segment_seconds` between runs invalidates the manifest and re-chunks (assert
  rechunk); existing `<name>.txt` → whole stage skipped.
- **Demo:** `uv run pytest tests/test_pipeline.py -q`.

### Task 5 — CLI plan + preflight (`--dry-run` / `check`)
- **Objective:** Surface the transcribe backend and validate prerequisites early.
- **Guidance:** In `__main__.py`, add `transcribe [openrouter:<model>]` to the
  plan string; **remove `"elevenlabs"` from `_ALWAYS_BINARIES`** (currently
  `("ffmpeg", "elevenlabs")`) so `check` no longer requires the legacy CLI (E3);
  in `check`/preflight, require `openrouter` present and the
  `openrouter.api_key_env` env var set (fail fast with an actionable message)
  before any recording work.
- **Tests (`test_main.py`):** `--dry-run` output contains the new label with the
  configured model; `check` no longer fails when `elevenlabs` is absent from
  `PATH`; `check` fails when the OpenRouter API-key env var is unset.
- **Demo:** `uv run transcriber --dry-run --config examples/acme.config.yaml`.

### Task 6 — Docs & examples
- **Objective:** Bring README + examples in line with the new transcriber.
- **Guidance (R17/R18):** README Pipeline step 3 (OpenRouter STT, diarized,
  chunked), Prerequisites (remove ElevenLabs CLI/account; `openrouter` always
  required; keep ffmpeg), Privacy/data-egress (audio egresses to OpenRouter every
  run; `usage.cost` metering), schema table (`transcribe.*` fields;
  `timeouts.transcribe`), and a new "config migration" note. Update both
  `examples/*.config.yaml`.
- **Tests (`test_examples.py`):** both examples load; `transcribe.model_id`
  is an OpenRouter slug; `openrouter` present; no `scribe_v1`/`elevenlabs`
  strings remain (grep-style assertion).
- **Demo:** `uv run pytest tests/test_examples.py -q`.

### Task 7 — Cleanup of ElevenLabs remnants + segment temp files
- **Objective:** Remove dead code/tests; ensure segment artifacts are cleaned.
- **Guidance:** Delete the elevenlabs argv/JSON code and any elevenlabs-specific
  tests, and remove `"elevenlabs"` from `_ALWAYS_BINARIES` if not already done in
  Task 5 (E3). In `cleanup.py`, add the per-recording work dir
  `transcribe_work.{name}/` (holding `part_*.mp3` + `segments.json`) to
  `intermediate_paths` as an `rmtree` candidate (keep `.txt` as a KEEP suffix).
  Grep the repo for `elevenlabs`/`scribe_v1` and remove stragglers (code only;
  historical `docs/` may retain references).
- **Tests (`test_cleanup.py`):** a `transcribe_work.<name>/` dir with `part_*.mp3`
  + `segments.json` is removed on cleanup; `<name>.txt` is preserved;
  `--keep-intermediates` keeps everything.
- **Demo:** `uv run pytest -q` (full suite green).

## Verification

Run `uv run pytest -q` after each task; the full suite must be green at Task 7.
For an end-to-end smoke test, a gated live transcription (behind an env flag with
a real `OPENROUTER_API_KEY` and a short multi-speaker clip) confirms the diarized
`<name>.txt` and logged `usage.cost`, but the unit suite must not depend on
network or secrets.

## Out of scope

Audio extraction (ffmpeg mp3), scene/slide extraction, the slides `openrouter`
vision backend, summarize backends (`agy`/`agno`), Notion, Telegram, and S3 are
unchanged. Word-level subtitle (SRT/VTT) output is not produced (OpenRouter STT
does not emit it; build from timestamps later if ever needed).
