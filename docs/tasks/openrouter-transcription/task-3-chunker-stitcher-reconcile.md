# Task 3 — Chunker + stitcher + speaker reconciliation

**Status:** [ ]

**Spec:** `docs/specs/openrouter-transcription.md`
**Dependencies:** Task 1 (config), Task 2 (`TranscriptChunk`/`SpeakerSegment`)

## Target
`transcriber/backends/transcribe_openrouter.py` (chunk/stitch/reconcile portion),
`tests/test_transcribe.py`.

## Change
- `chunk_audio(mp3, work_dir, segment_seconds, overlap_seconds, timeout)` →
  ordered part paths via the pipeline's `_run_command` ffmpeg seam, **one call
  per part** (the segment muxer has **no** overlap option — E1):
  `ffmpeg -ss <k*segment_seconds> -t <segment_seconds + overlap_seconds> -i <mp3>
  -c copy <work_dir>/part_{k:03d}.mp3`.
  - `work_dir` = `recordings_dir / f"transcribe_work.{name}"` (E2 — mirrors
    `extracted_slides.{name}`).
- `stitch(chunks)` offsets each chunk's segment times by its cumulative start
  (`k*segment_seconds`) and concatenates into a monotonic timeline (R8).
- `reconcile_speakers(chunks)` maps each chunk's per-request provider labels onto
  a **global speaker registry** using the overlap window (align overlapping-region
  segments by time/text). An unmatched speaker (silent in the overlap) gets a
  **new** global id + a **logged** warning — never force-merged or dropped
  (R9/E4).
- Render output: group consecutive same-global-speaker segments into
  `Speaker <ID>: <text>` lines **with no timestamp markers** (R2/P2). If no
  speaker labels anywhere (or `diarize=false`), write flat stitched text (R5).

## Constraints
- Pure/deterministic chunk+stitch+reconcile logic so it is unit-testable with
  synthetic fixtures (no real audio, no network).
- Use the pipeline's existing `_run_command` seam for ffmpeg so tests can
  monkeypatch it.

## Ownership
Owns the chunk/stitch/reconcile functions in `backends/transcribe_openrouter.py`
and their tests in `tests/test_transcribe.py`. Coordinate module boundaries with
Task 2.

## Observable Acceptance
- **Tests (synthetic `TranscriptChunk` fixtures, no real audio):**
  - 2–3 chunks where provider labels are **permuted** across seams →
    reconciliation yields consistent global speakers.
  - A speaker absent from an overlap window → **new** global id (asserted) +
    warning.
  - Timestamp offsets are monotonic across chunks.
  - No-speaker-labels / `diarize=false` input → flat stitched text.
  - `chunk_audio` with `_run_command` monkeypatched → assert **per-part
    `-ss`/`-t`** argv and the `transcribe_work.<name>` path (not a `-f segment`
    call).
- **Demo:** `uv run pytest tests/test_transcribe.py -q`.
