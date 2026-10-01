# Task 1 — Config reshape (`transcribe` + mandatory `openrouter` + timeouts)

**Status:** [ ]

**Spec:** `docs/specs/openrouter-transcription.md`
**Dependencies:** none (foundation — Tasks 2–7 import this model)

## Target
`transcriber/config.py` and `tests/test_config.py`.

## Change
- Extend `Transcribe` with:
  - `diarize: bool = True`
  - `segment_seconds: int = 480`
  - `overlap_seconds: int = 5`
  - change `model_id` default to `"microsoft/mai-transcribe-2"` (semantics change
    from an ElevenLabs id to an **OpenRouter STT slug**).
- `Timeouts`: rename `elevenlabs` → `transcribe` (default `DEFAULT_TIMEOUT_SECONDS`
  = 900). **Silently drop** a legacy `timeouts.elevenlabs` key from the input
  before building `Timeouts` so old host configs still load (R15).
- Make `openrouter` **unconditionally required** (R13): extend the
  `needs_openrouter` logic so a config without an `openrouter` section always
  raises `ConfigError` (keep the slides/`agno` reasons in the message, but require
  it for every config now).

## Constraints
- Follow the existing `_build_section` / present-or-`None` pattern.
- Secrets are env-var **names** only; never resolve or store secret values here.
- Config only — no backend/pipeline/orchestration logic.

## Ownership
Owns: `transcriber/config.py`, `tests/test_config.py`. Do not edit
`pipeline.py`, `backends/*`, `__main__.py`, `cleanup.py`, or examples (Task 6).

## Observable Acceptance
- **Tests (`test_config.py`):**
  - A config **without** `openrouter` now raises `ConfigError` (R13).
  - `transcribe` defaults populate (`diarize=True`, `segment_seconds=480`,
    `overlap_seconds=5`, `model_id="microsoft/mai-transcribe-2"`).
  - A config with legacy `timeouts.elevenlabs` loads and the value is ignored;
    `timeouts.transcribe` defaults to 900 and is overridable.
  - `segment_seconds` / `overlap_seconds` / `diarize` round-trip from YAML and JSON.
- **Demo:** `uv run python -c "from transcriber.config import load; print(load('examples/acme.config.yaml').transcribe)"`.
