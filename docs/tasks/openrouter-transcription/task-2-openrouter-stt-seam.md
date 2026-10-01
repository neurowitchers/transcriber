# Task 2 — OpenRouter STT seam (`_openrouter_transcribe`)

**Status:** [x]

**Spec:** `docs/specs/openrouter-transcription.md`
**Dependencies:** Task 1 (config fields)

## Target
`transcriber/backends/transcribe_openrouter.py` *(new)*,
`transcriber/backends/errors.py`, `tests/test_transcribe.py` *(new)*.

## Change
- Add `TranscribeError(RuntimeError)` to `errors.py` (same log-hygiene contract
  as `SlideDescribeError`).
- New `_openrouter_transcribe(config, audio_path, *, model, diarize, language,
  timeout) -> TranscriptChunk`. Copy the retry/backoff/`_snippet`/log-hygiene
  scaffolding from `backends/openrouter.py`.
  - `POST {base_url}/audio/transcriptions` as **JSON base64**
    (`Content-Type: application/json`; base64-encode the file as **raw base64,
    not a `data:` URI**; the JSON path, not OpenAI multipart — P3).
  - When `diarize` is true: `response_format: "verbose_json"` + the per-model
    diarization toggle from a small mapping (start with `mai-transcribe-2` →
    `provider.options.azure.diarization.enabled: true`).
  - When `diarize` is false: **omit** the provider toggle; emit flat text (P1).
  - Parse `segments`/`words`/`usage.cost` defensively into `SpeakerSegment`s;
    fall back to flat `text` with a logged warning (R5).
  - Retry `{429,500,502,503,504}` with the bounded backoff; a `400` →
    `TranscribeError` with **no** retry (R10). Never log key/headers/audio.

## Data types (define here or in `interfaces`)
```python
@dataclass
class SpeakerSegment: speaker: str; start: float; end: float; text: str
@dataclass
class TranscriptChunk: index: int; offset: float; segments: list[SpeakerSegment]; cost: float; raw_text: str
```

## Constraints
- No chunking/stitching here (Task 3) — this seam handles exactly **one** audio
  file and returns one `TranscriptChunk`.
- Log hygiene (R3): never log the API key, headers, or base64 payload.

## Ownership
Owns: `backends/transcribe_openrouter.py` (seam portion), `backends/errors.py`,
`tests/test_transcribe.py`. Coordinate the new module file with Task 3 (which adds
chunk/stitch/reconcile into the same module) — agree function boundaries.

## Observable Acceptance
- **Tests (mock `httpx`):**
  - 200 with `segments` → parsed speakers.
  - 200 with only `text` → flat fallback + logged warning.
  - `diarize=false` → request body carries **no** provider diarization block and
    output has no `Speaker` prefixes.
  - `400` → `TranscribeError` (no retry).
  - `429`→`200` → retried then succeeds.
  - the key/base64 never appear in logs or the exception message.
- **Demo:** `uv run pytest tests/test_transcribe.py -q`.
