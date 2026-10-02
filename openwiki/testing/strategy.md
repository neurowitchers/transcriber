---
type: "Reference"
title: "Transcriber Test Strategy"
openwiki_generated: true
verified:
  - by: openwiki/0.6.1
    at: 2026-10-02T19:10:14.922Z
sources:
  - id: openwiki-source-8b176c94b018259ee14f35b7
    resource: repo://tests/test_main.py
  - id: openwiki-source-df4110b2c5338913ae9eedcf
    resource: repo://transcriber/__main__.py
  - id: openwiki-source-c4777b8db8d4806695ac8b6a
    resource: repo://transcriber/config.py
generated: { by: "openwiki/0.6.1", at: "2026-10-02T19:10:14.922Z" }
---


# Transcriber Test Strategy

Testing for the transcriber CLI and orchestrator is intentionally narrow and opinionated: **no real binaries, network calls, agents, or publishing destinations are exercised in tests**. Every external is replaced with a stub at a well-defined seam, so the tests stay focused on wiring, ordering, idempotency, failure isolation, config validation, log hygiene, and the absence of secrets in outputs.

<!-- openwiki: broken internal link [/tests/test_main.py] link "/tests/test_main.py" is root-absolute, which no real consumer resolves against the repository root (not a coding agent reading the page, not GitHub's Markdown renderer, not a local viewer); use a path relative to this file instead. Fix the href or restore the target, then delete this comment. -->
The primary source of truth for what is covered and how externals are isolated is [`/tests/test_main.py`](/tests/test_main.py).

## What the test suite covers

The test file’s own docstring states the coverage pyramid:

- pre-flight failure on a missing binary and on a missing env var
- `--dry-run` prints a plan and performs no side effects
- happy path over 2 recordings (all stages called, in order)
- failure isolation (one recording fails → its cleanup skipped, the other still processes fully)
- the state manifest causes an already-complete stage to be skipped on re-run
- disabled toggles (slides / parse / s3) skip their stages

Beyond that, the suite also covers:

- the `transcriber check` subcommand exit codes
- backend-aware pre-flight requirements (OpenRouter unconditionally required, Notion token only for `agno`, `agy` only when used, `aws` only when S3 enabled)
- missing `openrouter` section fails fast
- dry-run plan token pinning and disambiguation (`scene-extract` vs `describe-slides`, `summarize+notion [<backend>]`)
- describe_slides backend selection and gating (including rejection of invalid backends)
- summarize receiving slide markdown vs `None`, and not receiving image paths
- discovery excluding recordings that already have a summary `.md`
- disabled-slides regression: a stale `<name>.slides.md` must not be fed into the summarize backend
- `debug: true` behaves like `--keep-intermediates` for cleanup
- shared OpenRouter block with distinct models per stage

## Isolation strategy: monkeypatch at the stage-call seam

<!-- openwiki: broken internal link [/tests/test_main.py] link "/tests/test_main.py" is root-absolute, which no real consumer resolves against the repository root (not a coding agent reading the page, not GitHub's Markdown renderer, not a local viewer); use a path relative to this file instead. Fix the href or restore the target, then delete this comment. -->
The central test helper is the `stage_calls` fixture in [`/tests/test_main.py`](/tests/test_main.py). It monkeypatches the stage functions and records calls in order:

- `process_recording` (pipeline)
- `_get_slides_backend` / `get_summarize_backend` (backend selection seams)
- `TelegramPublisher`
- `s3.sync`
- `cleanup`

The summarize backend stub is more than a no-op: it writes the summary `.md` and `<name>.telegram.md` so downstream stages behave as they would in a real run (Telegram reading the digest, discovery treating the recording as done, etc.). Backend selection is stubbed at the orchestrator seam (`_get_slides_backend` / `get_summarize_backend`) so no real `agy` / OpenRouter / `agno` internals are exercised. That keeps the test to the wiring surface rather than the backend implementations.

Externals are isolated at these seams in the production code:

<!-- openwiki: broken internal link [/transcriber/__main__.py] link "/transcriber/__main__.py" is root-absolute, which no real consumer resolves against the repository root (not a coding agent reading the page, not GitHub's Markdown renderer, not a local viewer); use a path relative to this file instead. Fix the href or restore the target, then delete this comment. -->
- Pipeline binaries: [`/transcriber/__main__.py`](/transcriber/__main__.py) relies on `shutil.which` for pre-flight and on the pipeline’s `_run_command` for media stages; tests replace `shutil.which` and the stage functions directly.
- Backend selection: `_get_slides_backend` and `get_summarize_backend` exist specifically as pure functions of config that can be swapped in tests.
- Publishing: `TelegramPublisher` and `s3.sync` are the publish seams.
- Cleanup: `cleanup.cleanup` is the deletion seam.

<!-- openwiki: broken internal link [/transcriber/config.py] link "/transcriber/config.py" is root-absolute, which no real consumer resolves against the repository root (not a coding agent reading the page, not GitHub's Markdown renderer, not a local viewer); use a path relative to this file instead. Fix the href or restore the target, then delete this comment. -->
The configuration and secret model is covered through [`/transcriber/config.py`](/transcriber/config.py): secrets are referenced by env-var name only and resolved at use time via `resolve_env`, and the loader validates constrained fields and the now-mandatory `openrouter` section.

## Test families

### Pre-flight failure isolation

<!-- openwiki: broken internal link [/tests/test_main.py] link "/tests/test_main.py" is root-absolute, which no real consumer resolves against the repository root (not a coding agent reading the page, not GitHub's Markdown renderer, not a local viewer); use a path relative to this file instead. Fix the href or restore the target, then delete this comment. -->
Pre-flight tests in [`/tests/test_main.py`](/tests/test_main.py) cover:

- missing binary → problem mentions the binary and “binary”
- missing env var → problem mentions the variable name and “environment variable”
- `check` subcommand exits `1` when missing and `0` when present
- `aws` is required only when S3 is enabled
- OpenRouter API key is required on every run regardless of slides/summary backend
- missing `openrouter` section fails fast with an actionable message
- Notion token required iff `summary.backend == "agno"`
- `agy` required iff a stage uses it
- `elevenlabs` is no longer in `_ALWAYS_BINARIES` and absent from PATH no longer fails `check`

These tests assert on the human-readable problem text, not just on boolean outcomes, because the messages are part of the contract.

### Dry-run: plan printed, no side effects

Dry-run tests assert:

- exit code `0`
- output contains `Execution plan`, each recording name, Notion, Telegram, and the transcribe token with the OpenRouter backend and model
- no stage functions are invoked (`stage_calls["calls"] == []`)
- no `*.transcriber_state.json` manifests are written

Dry-run is intentionally **not** pre-flight gated in the implementation, and the tests reflect that: they assert `rc == 0` even when binaries/env vars are missing.

### Happy path and ordering

Happy-path tests run two recordings and assert the exact ordered call sequence, including:

- pipeline → describe_slides → summarize/agent → telegram → cleanup for each recording
- s3 inserted between telegram and cleanup when enabled
- `debug: true` skips cleanup while the rest of the pipeline still runs

These tests use `stage_calls["calls"]` as the authoritative record of orchestrated behavior.

### Failure isolation

Failure isolation tests make one recording fail at the summarize stage and assert:

- overall exit code `1`
- the failed recording does not get telegram or cleanup
- the other recording processes fully, including cleanup
- the failed recording’s manifest records pipeline complete but not summarize/cleanup

This validates that the batch continues and that cleanup is conditional on full success.

### State manifest and idempotency

Manifest tests pre-mark stages complete and assert that already-complete stages are skipped on re-run, including:

- pipeline/slides/agent/telegram skipped when already complete; only cleanup runs
- describe_slides skipped when manifest says complete, even if the artifact is present
- an artifact-present-but-manifest-incomplete case skips the paid backend call and marks the stage complete

These tests make the manifest the source of truth, not artifact existence alone.

### Disabled toggles

Toggle tests assert that disabling slides or S3 removes the corresponding stages from the plan and from execution, and that a stale `<name>.slides.md` is never fed into the summarize backend when slides are disabled.

### Config and schema drift guards

Config is exercised through YAML-written configs in many end-to-end tests, and through direct `Config` construction in unit-style tests. The loader validation has its own protection:

- `stages.slides` must be a mapping with `{enabled, backend}`; the legacy bare-boolean form is rejected
- `summary.language` must be `"en"` or `"original"`
- `stages.slides.backend` must be one of the allowed slides backends
- `summary.backend` must be one of the allowed summary backends
- `openrouter` is now unconditionally required at load time
- `notion.token_env` is required when `summary.backend == "agno"`
- `debug` must be a boolean

These guards are intended to catch config-schema drift early, and the tests treat them as part of the contract rather than as incidental validation.

### Log hygiene and secret absence

Log hygiene and secret absence are part of test intent in this suite, even though they are often asserted indirectly through behavior:

- pre-flight assertions check problem text for variable names, not values
- tests monkeypatch env vars and assert on names like `TG_TOKEN` or `OPENROUTER_API_KEY`
- the summarize backend stub receives and records `slides_markdown`, and the suite asserts that what reaches the backend is text (not image paths) and `None` when slides are disabled

The underlying design intent is that secrets are referenced by env-var name only and resolved at use time, so test assertions focus on names and behavior rather than on secret values.

## How to read the test file as documentation

<!-- openwiki: broken internal link [/tests/test_main.py] link "/tests/test_main.py" is root-absolute, which no real consumer resolves against the repository root (not a coding agent reading the page, not GitHub's Markdown renderer, not a local viewer); use a path relative to this file instead. Fix the href or restore the target, then delete this comment. -->
If you want to know whether a behavior is tested, start from [`/tests/test_main.py`](/tests/test_main.py) and read the fixture and the family of tests around it. The `stage_calls` fixture defines the observable seam; the pre-flight section defines how binaries and env vars are validated; the dry-run section defines the plan contract; the happy-path and failure-isolation sections define batch semantics; the manifest and toggle sections define idempotency and gating; and the backend-selection and config sections define the validation surface.

For the production-side explanation of why these seams exist, see:

- [OSS Companion Transcriber Engine](../architecture/oss-companion-transcriber.md)
- [Pre-flight checks and idempotent retries](../operations/pre-flight-and-idempotency.md)
