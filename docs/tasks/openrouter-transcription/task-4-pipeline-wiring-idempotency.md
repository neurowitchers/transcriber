# Task 4 — Pipeline wiring + per-segment idempotency

**Status:** [x]

**Spec:** `docs/specs/openrouter-transcription.md`
**Dependencies:** Task 1, Task 2, Task 3

## Target
`transcriber/backends/transcribe_openrouter.py` (`transcribe_recording` entry),
`transcriber/pipeline.py`, `tests/test_pipeline.py`.

## Change
- Add `transcribe_recording(mp3, txt, config)` orchestrating:
  chunk → (manifest check → seam call → persist) per segment → stitch →
  reconcile → write `<name>.txt`.
- Persist the manifest `recordings_dir / f"transcribe_work.{name}" /
  "segments.json"` storing **`segment_seconds` + `overlap_seconds`** plus per-part
  transcript + done flag (R11). On retry:
  - skip done parts (re-issue the paid call **only** for failed/missing parts);
  - if stored params differ from current config → **discard the work dir and
    rechunk** (E5).
- In `pipeline.py`, `_transcribe` delegates to `transcribe_recording`; **remove**
  the `elevenlabs` argv + JSON post-processing; keep the `txt.exists()`
  stage-level skip. Thread `config` through `process_recording` (not just
  `model_id`).

## Constraints
- Keep the artifact contract: durable `<name>.txt` (speaker-attributed per Task 3).
- Retries must never re-pay for a completed segment (manifest-gated).

## Ownership
Owns: `transcribe_recording`, `pipeline.py` transcribe path, `tests/test_pipeline.py`.

## Observable Acceptance
- **Tests (`test_pipeline.py`, monkeypatch the seam):**
  - A 2-segment recording writes a diarized `<name>.txt`.
  - A simulated failure on segment 2, then rerun → the seam is re-called **only**
    for segment 2 (assert call count).
  - Changing `segment_seconds` between runs invalidates the manifest and
    re-chunks (assert rechunk).
  - An existing `<name>.txt` → whole stage skipped.
- **Demo:** `uv run pytest tests/test_pipeline.py -q`.
