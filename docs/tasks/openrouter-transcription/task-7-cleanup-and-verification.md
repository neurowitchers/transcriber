# Task 7 — Cleanup of ElevenLabs remnants + segment temp files + full verification

**Status:** [x]

**Spec:** `docs/specs/openrouter-transcription.md`
**Dependencies:** Tasks 1–6 (verification); cleanup edits need Task 4's work-dir shape

## Target
`transcriber/cleanup.py`, `tests/test_cleanup.py`, repo-wide grep.

## Change (R19)
- Delete the elevenlabs argv/JSON code paths and any elevenlabs-specific tests.
- Remove `"elevenlabs"` from `_ALWAYS_BINARIES` if not already done in Task 5 (E3).
- In `cleanup.py`, add the per-recording work dir `transcribe_work.{name}/`
  (holding `part_*.mp3` + `segments.json`) to `intermediate_paths` as an `rmtree`
  candidate, like `extracted_slides.{name}/`. Keep `.txt` as a **KEEP** suffix.
- Grep the repo for `elevenlabs` / `scribe_v1` and remove stragglers (code only;
  historical `docs/` critiques/specs may retain references).

## Ownership
Owns: `cleanup.py`, `tests/test_cleanup.py`. Final full-suite verification is this
task's responsibility.

## Observable Acceptance
- **Tests (`test_cleanup.py`):**
  - A `transcribe_work.<name>/` dir with `part_*.mp3` + `segments.json` is removed
    on cleanup.
  - `<name>.txt` is preserved.
  - `--keep-intermediates` keeps everything.
- **Full verification:** `uv run pytest -q` is green across the whole suite.
- **Smoke (optional, gated):** a live transcription behind an env flag with a real
  `OPENROUTER_API_KEY` and a short multi-speaker clip produces a diarized
  `<name>.txt` and a logged `usage.cost`. The unit suite must not depend on
  network or secrets.
- **Demo:** `uv run pytest -q`.
