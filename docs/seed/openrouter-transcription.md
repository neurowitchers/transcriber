# Seed: Replace ElevenLabs transcription with OpenRouter STT

## Intent

The engine already depends on **OpenRouter** (slides vision backend, and the
`agno` summarize backend). Transcription currently goes through a **separate
ElevenLabs** account + external CLI (`elevenlabs speech-to-text convert`). That
second vendor is redundant and not cost-effective. I want to **drop ElevenLabs
entirely** and transcribe through OpenRouter's speech-to-text endpoint, reusing
the OpenRouter wiring we already have.

## What it should do

Replace the `transcribe` stage so it calls OpenRouter's
`POST /api/v1/audio/transcriptions` (same base URL + API key we already use for
chat/vision) instead of shelling out to the `elevenlabs` CLI. The stage still
produces the same `<name>.txt` artifact the summarize stage consumes — now
**speaker-attributed** (diarized).

Because meeting recordings are long and OpenRouter has a ~60-second upstream
processing timeout (and a 25 MB per-call cap), the stage must **split the audio
into chunks, transcribe each, and stitch the results back together** in order —
including reconciling speaker labels across chunk boundaries. Chunking uses the
`ffmpeg` already on `PATH` (no new dependency); the HTTP call reuses the
existing `httpx` + retry/backoff + log-hygiene seam (no new dependency).

## Key decisions (from brainstorm)

- **Fully replace ElevenLabs.** No `elevenlabs` transcribe backend is retained;
  the `elevenlabs` CLI stops being a prerequisite. (Not cost-effective to keep a
  second STT vendor when OpenRouter is already configured.)
- **Chunk + stitch for meeting-length audio.** Segment `<name>.mp3` with ffmpeg
  (`-f segment -segment_time`, deterministic part names, a small overlap window),
  transcribe each segment, stitch in index order, offset timestamps by cumulative
  chunk start.
- **Accuracy matters; diarization is required.** This rules out plain Whisper
  (`whisper-large-v3` / `-turbo` do **not** diarize on OpenRouter). Diarization
  needs `response_format: "verbose_json"` plus a provider-specific toggle.
  - **Default model: `microsoft/mai-transcribe-2`** — #1 on FLEURS multilingual,
    explicit speaker diarization, word-level timestamps, keyword biasing for
    meeting jargon, cheap per-hour pricing. Operator-overridable to any
    diarization-capable slug (e.g. Google Gemini 3.5 Transcribe, Deepgram
    Nova-3, Grok STT 1.0, Fish Audio Transcribe 1 Pro).
  - The response parser reads `segments`/`words` (speaker labels) and renders
    speaker-attributed text; it falls back defensively to flat `text` with a
    logged warning if a provider omits the richer shape.
- **All-OpenRouter egress is acceptable.** No need to preserve a
  "zero-OpenRouter" transcription path. Every run now egresses audio to
  OpenRouter and its routed upstream providers.
- **`openrouter` config becomes required** for every run (it is now the
  transcriber), not just when slides are enabled or `summary.backend == "agno"`.
- **No silent fallback** (keep the engine's existing rule): if the configured
  model can't diarize, OpenRouter returns a 400 → fail the stage with an
  actionable error (surfaced in `check`). A retry re-issues only the failed
  chunk's paid call.
- **Idempotency:** a per-segment manifest/sidecar records completed chunks so a
  retry re-transcribes only failed chunks (mirrors the existing manifest-gated,
  no-re-issue-of-completed-paid-calls rule). Segment temp files are cleaned up
  with the other intermediates unless `--keep-intermediates`.

## Config changes (illustrative, not final)

```yaml
transcribe:
  model_id: microsoft/mai-transcribe-2   # OpenRouter STT slug (was scribe_v1)
  diarize: true                          # speaker labels (required)
  segment_seconds: 480                   # chunk length, sized under the 60s/25MB limits
openrouter:                              # now REQUIRED for every run
  api_key_env: OPENROUTER_API_KEY
  # base_url default https://openrouter.ai/api/v1
timeouts:
  transcribe: 900                        # renamed from `elevenlabs`
```

- `transcribe.model_id` semantics change: ElevenLabs id (`scribe_v1`) →
  **OpenRouter STT slug**.
- `timeouts.elevenlabs` → `timeouts.transcribe` (handle the legacy key per the
  project's migration policy — silently ignore or hard-error).
- `openrouter` moves from conditionally-required to **always required**.

## Request/response contract (reference)

- `POST {base_url}/audio/transcriptions`, Bearer key resolved from
  `openrouter.api_key_env` at use time (never logged).
- Body: `{ model, input_audio: { data: <raw base64 mp3, not a data: URI>,
  format: "mp3" }, language?, response_format: "verbose_json" }` plus the
  provider-specific diarization toggle (e.g.
  `provider.options.azure.diarization.enabled: true` for `mai-transcribe-2`).
- Response: `{ text, segments?, words?, usage }`. Log aggregate `usage.cost`
  per run; never log audio bytes, the API key, or headers.
- Limits to design around: ~60 s processing timeout, 25 MB per call, no audio
  URLs, no SRT/VTT output.

## Follow-up tasks

- **CLI surface:** `--dry-run` / `check` plan label `transcribe
  [openrouter:<model>]`; `check` validates `openrouter` presence and a
  diarization-capable model.
- **README:** rewrite the Pipeline (transcribe stage), Prerequisites (remove the
  ElevenLabs CLI + account; OpenRouter now always required), Privacy/data-egress
  (audio now egresses to OpenRouter on every run), and the "Host maintainers —
  config migration" note (ElevenLabs removed; `model_id` is now an OpenRouter
  slug; `openrouter` always required; `timeouts.elevenlabs` → `timeouts.transcribe`).
- **Examples:** update both `examples/*.config.yaml` — drop `scribe_v1`, add the
  OpenRouter STT block, remove any ElevenLabs references.
- **Cleanup:** delete dead ElevenLabs code paths/tests; ensure segment temp
  files are removed unless `--keep-intermediates`.

## Risks to carry into the full spec

- **Cross-chunk speaker-label drift** — providers number speakers per request,
  so chunk 2's "Speaker 1" may be chunk 1's "Speaker 2". Reconcile via an overlap
  window + label matching; document the heuristic and make it unit-testable with
  synthetic multi-chunk fixtures.
- **`verbose_json` shape varies by provider** — centralize parsing in one seam
  with defensive extraction; tolerate missing `words`, fall back to `segments`,
  then flat `text`.
- **Per-request cost on long audio** — read and log `usage.cost` per chunk;
  document the cost model.
