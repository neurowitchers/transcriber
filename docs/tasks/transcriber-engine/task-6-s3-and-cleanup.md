# Task 6 — S3 sync (`publish/s3.py`) + cleanup (`cleanup.py`)

**Status:** [ ]

**Spec:** `docs/specs/transcriber-engine.md`
**Dependencies:** Task 1 (config model). Independent of Tasks 2/3/4/5 — parallelizable.

## Target
`transcriber/publish/s3.py` and `transcriber/cleanup.py`.

## Change
- **S3 sync (`publish/s3.py`):** `aws s3 sync <config.s3.bucket>` with `--profile <config.s3.profile>` via `duct`, wrapped with a configurable timeout (`config.timeouts.s3`, E3). Gated on `config.stages.s3_sync`.
- **Cleanup (`cleanup.py`):** delete intermediate artifacts (`.mp3`, `.jsonl`, `.txt`, `extracted_slides.*`, `*.scenes.csv`); **keep** the source `.mp4` and the final `.md`.
  - Cleanup runs **only after** a recording's successful publish and (if enabled) successful S3 sync — enforce sync-before-delete ordering.
  - A `--keep-intermediates` flag disables deletion.

## Constraints
- Never delete `.mp4` or `.md`.
- No cleanup on failure or when `--keep-intermediates` is set.
- If S3 is enabled, upload must complete successfully before any deletion.

## Ownership
Owns: `transcriber/publish/s3.py`, `transcriber/cleanup.py`, and their tests. Coordinate the `publish/` package `__init__` with Task 5 — additive only.

## Observable Acceptance
- **Tests (pytest):** sync command line correct and skipped when disabled; cleanup deletes exactly the intermediate set and preserves `.mp4`/`.md`; no cleanup on failure or with `--keep-intermediates`; sync-before-delete ordering enforced.
- **Demo:** a dir with all artifacts → after a successful run only `.mp4` + `.md` remain (all remain with `--keep-intermediates`).
