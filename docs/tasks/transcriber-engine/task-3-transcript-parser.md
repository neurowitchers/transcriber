# Task 3 — Transcript parser module (`parse.py`)

**Status:** [ ]

**Spec:** `docs/specs/transcriber-engine.md`
**Dependencies:** none (self-contained; Task 2/Task 7 call it). Independent — parallelizable.

## Target
`transcriber/parse.py`.

## Change
Port `the host A Python transcript parser` into an importable Python function (no `uv run` shell-out):
- Read an ElevenLabs JSONL transcript; collect `word` and `spacing` items with their `start` time and `text`.
- Emit readable text with `[MM:SS]` timestamp markers inserted at a configurable interval (default 15s), matching the source behavior.
- Provide a function usable from the pipeline (input path → output `.txt` path, plus interval arg).

## Constraints
- Preserve the source's semantics exactly: skip empty/malformed JSONL lines; interval-boundary marker placement identical to the original.
- Pure module — no subprocess calls, no config coupling beyond the interval argument.

## Ownership
Owns: `transcriber/parse.py` and its tests, plus test fixtures (sample `.jsonl` and golden `.txt`).

## Observable Acceptance
- **Tests (pytest):** golden-file test — sample `.jsonl` → expected timestamped `.txt`; empty/malformed lines skipped; interval boundary correct.
- **Demo:** parse a sample `.jsonl` to `.txt` and diff against the golden output.
