# Task 2 — Deterministic media pipeline (`pipeline.py`)

**Status:** [ ]

**Spec:** `docs/specs/transcriber-engine.md`
**Dependencies:** Task 1 (config model); integrates Task 3 (`parse.py`) at wiring time but can be built against a stub.

## Target
`transcriber/pipeline.py`.

## Change
Port the deterministic media stages from `the host A PowerShell transcription script` to Python using `duct`:
1. **Audio extract:** mp4 → mp3 via `ffmpeg` (mirror source flags: `-vn -c:a libmp3lame -q:a 2`, `-hide_banner -loglevel error`).
2. **Slide/scene extraction (optional, when `stages.slides`):** `scenedetect -b pyav -i <mp4> -o extracted_slides.<name> detect-content --threshold 30 --min-scene-len 5s list-scenes -f <name>.scenes.csv save-images -n 1`.
3. **Transcribe:** `elevenlabs speech-to-text convert --file <mp3> --model-id <config.transcribe.model_id> --format jsonl` → `<name>.jsonl`.
- Wrap **every** child-process call with a configurable execution timeout from `config.timeouts` (E3).
- Idempotent per-artifact guards: skip a step whose output already exists.
- Return a structured per-recording result listing which artifacts are new.

## Constraints
- Mirror the exact ffmpeg/scenedetect/elevenlabs arguments from the source scripts.
- Timeouts come from config with sensible defaults.
- Transcript parsing (`.jsonl` → `.txt`) is **Task 3's** function — call it if present, else leave for wiring.
- Windows-first path handling.

## Ownership
Owns: `transcriber/pipeline.py` and its tests. Do not own `parse.py`, `agent.py`, or the orchestrator.

## Observable Acceptance
- **Tests (pytest, `duct` mocked):** assert command lines/args per toggle; existing-artifact skip logic; new-recording detection; timeout value passed through / enforced.
- **Demo:** run on a fixture `.mp4` (or mocked binaries) → `.mp3`/`.jsonl` (+ slides when enabled) appear; a re-run is a no-op.
