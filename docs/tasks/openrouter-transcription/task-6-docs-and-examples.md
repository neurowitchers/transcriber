# Task 6 — Docs & examples

**Status:** [x]

**Spec:** `docs/specs/openrouter-transcription.md`
**Dependencies:** Task 1 (frozen field set)

## Target
`README.md`, `examples/example.config.yaml`, `examples/acme.config.yaml`,
`tests/test_examples.py`.

## Change (R17 / R18)
- **README:**
  - Pipeline step 3 → OpenRouter STT (diarized, chunked via explicit time slices).
  - Prerequisites → **remove** the ElevenLabs CLI + account; state `openrouter`
    is now always required; keep `ffmpeg`.
  - Privacy / data-egress → audio egresses to OpenRouter on **every** run;
    `usage.cost` metering; downscaling note not applicable to audio.
  - Schema table → add `transcribe.diarize`, `transcribe.segment_seconds`,
    `transcribe.overlap_seconds`; `model_id` is an OpenRouter STT slug;
    `timeouts.transcribe` (was `timeouts.elevenlabs`); `openrouter` now required.
  - "Host maintainers — config migration" note: ElevenLabs removed; `model_id`
    now an OpenRouter slug; `openrouter` always required; `timeouts.elevenlabs`
    → `timeouts.transcribe` (legacy key silently ignored).
- **Examples:** drop `scribe_v1`; add the OpenRouter STT config
  (`model_id: microsoft/mai-transcribe-2`, `diarize: true`, `segment_seconds`,
  `overlap_seconds`); ensure the `openrouter` block is present; remove all
  ElevenLabs references.

## Ownership
Owns: `README.md`, `examples/*.config.yaml`, `tests/test_examples.py`.

## Observable Acceptance
- **Tests (`test_examples.py`):**
  - Both examples load.
  - `transcribe.model_id` is an OpenRouter slug; `openrouter` present.
  - No `scribe_v1` / `elevenlabs` strings remain in the example configs
    (grep-style assertion).
- **Demo:** `uv run pytest tests/test_examples.py -q`.
