# Task 7 — Orchestrator, CLI entry & pre-flight check (`__main__.py`)

**Status:** [ ]

**Spec:** `docs/specs/transcriber-engine.md`
**Dependencies:** Tasks 1–6 (integration task — wires all modules together). Run last (or after its dependencies land).

## Target
`transcriber/__main__.py` (and a small `state.py` helper for the status manifest if useful).

## Change
Implement the CLI + orchestrator: `uv run transcriber [--config PATH] [--keep-intermediates] [--dry-run]` plus a `transcriber check` subcommand.

1. **Pre-flight check (E2):** verify required binaries (`ffmpeg`, `scenedetect`, `elevenlabs`, `aws`, `agy`) are on `PATH` and required env vars (per enabled stages) are set; fail fast with a clear message. Also runnable standalone as `transcriber check`.
2. Run all child commands via `duct` with configurable execution timeouts (E3).
3. **`--dry-run` (P1):** load config, discover new `*.mp4`, print the execution plan (recordings, stages, publishing destinations), then exit — no subprocesses, no agent, no publishing, no deletion.
4. **Batch execution:** load config → discover new `*.mp4` → pipeline (Task 2/3) → agent (Task 4) → Telegram (Task 5) → S3 + cleanup (Task 6), per recording, with:
   - structured logging,
   - **per-recording `.transcriber_state.json` stage-status tracking (E1)** for resumeability — on retry, stages already marked complete (summarize / notion / telegram / s3) are skipped to avoid duplicate Notion pages / Telegram messages or skipped publishes,
   - per-recording failure isolation (one recording's failure does not abort the batch; cleanup only on that recording's success).
5. Emit a batch run summary (per-recording outcome, e.g. "2 succeeded, 1 failed at audio-extract").

## Constraints
- Cleanup only on a recording's full success (delegates to Task 6).
- The state manifest is the source of truth for stage completion — not artifact existence alone.
- Failure isolation must be real: a raised exception in one recording is caught, recorded, and the batch continues.

## Ownership
Owns: `transcriber/__main__.py`, the state-manifest helper, and integration/E2E tests. Imports the other modules; do not reimplement their internals.

## Observable Acceptance
- **Tests (pytest, externals mocked):** pre-flight failure on missing binary/env var; timeout enforcement; `--dry-run` prints the plan and performs no side effects; E2E happy path over 2 fixtures; a failing stage on one recording skips its cleanup but processes the other; state manifest causes completed stages to be skipped on re-run; disabled toggles skip stages.
- **Demo:** `uv run transcriber --config examples/acme.config.yaml --dry-run` prints the plan; a full run over fixtures reports per-recording results.
