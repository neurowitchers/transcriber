# Task 5 — CLI plan + preflight (`--dry-run` / `check`)

**Status:** [ ]

**Spec:** `docs/specs/openrouter-transcription.md`
**Dependencies:** Task 1 (config)

## Target
`transcriber/__main__.py`, `tests/test_main.py`.

## Change
- Add the transcribe backend to the plan string: `transcribe [openrouter:<model>]`
  (model from `config.transcribe.model_id`).
- **Remove `"elevenlabs"` from `_ALWAYS_BINARIES`** (currently
  `("ffmpeg", "elevenlabs")` at `__main__.py` ~line 99) so `check` no longer
  requires the legacy CLI (E3). `ffmpeg` remains the only always-required binary;
  `scenedetect` stays slides-gated.
- In `check`/preflight: require `openrouter` present and the env var named by
  `openrouter.api_key_env` set — fail fast with an actionable message before any
  recording work (R16).

## Constraints
- Preflight only validates; it must not perform any transcription or network I/O.

## Ownership
Owns: `__main__.py` (plan + preflight), `tests/test_main.py`.

## Observable Acceptance
- **Tests (`test_main.py`):**
  - `--dry-run` output contains `transcribe [openrouter:<model>]` with the
    configured model.
  - `check` no longer fails when `elevenlabs` is absent from `PATH`.
  - `check` fails when the OpenRouter API-key env var is unset.
- **Demo:** `uv run transcriber --dry-run --config examples/acme.config.yaml`.
