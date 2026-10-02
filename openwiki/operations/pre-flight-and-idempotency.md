---
type: operation
title: Pre-flight checks and idempotent retries
description: How the transcriber CLI validates binaries and env vars before work, prints a dry-run plan with no side effects, and uses a per-recording state manifest to skip already-complete stages on re-run while isolating failures.
tags: [transcriber, preflight, dry-run, idempotency, manifest, state, failure-isolation, orchestrator]
verified:
  - by: openwiki/0.6.1
    at: 2026-10-02T19:10:14.922Z
sources:
  - id: openwiki-source-8b176c94b018259ee14f35b7
    resource: repo://tests/test_main.py
  - id: openwiki-source-df4110b2c5338913ae9eedcf
    resource: repo://transcriber/__main__.py
  - id: openwiki-source-0c6dbb86c6b6001bf65f1b4c
    resource: repo://transcriber/state.py
generated: { by: "openwiki/0.6.1", at: "2026-10-02T19:10:14.922Z" }
---

# Pre-flight checks and idempotent retries

The transcriber CLI (`/transcriber/__main__.py`) has three related reliability surfaces that keep runs safe to retry: a **pre-flight check** that validates binaries and environment before any work, a **dry-run mode** that prints the execution plan without side effects, and a **per-recording state manifest** that makes re-runs skip already-complete stages. Together these mean a batch can be interrupted, re-run, or partially failed without duplicating paid/network calls, Notion pages, Telegram sends, or S3 syncs.

## Pre-flight check (E2)

`preflight_check(config)` returns a list of human-readable problem strings (empty when all is well). It is called in two places: inside the `transcriber check` subcommand and at the start of a normal run, where a non-empty result short-circuits to exit code 1 before any stage runs.

### Required binaries

The always-required binary set is a single tuple:

```python
_ALWAYS_BINARIES = ("ffmpeg",)
```

`ffmpeg` is the only binary required on every run. Transcription now runs over the OpenRouter HTTP seam rather than a legacy CLI, so `elevenlabs` is no longer in the always-set and `ffmpeg` is the sole unconditional binary. `scenedetect` is added when slides are enabled, `agy` when the summarize backend is `agy`, and `aws` when S3 sync is enabled.

The helper `_required_binaries(config)` builds the per-config list:

```python
def _required_binaries(config: Config) -> list[str]:
    required = list(_ALWAYS_BINARIES)          # ffmpeg
    if config.stages.slides.enabled:
        required.append("scenedetect")
    if _uses_agy(config):                       # summary.backend == "agy"
        required.append("agy")
    if s3_mod.is_enabled(config):
        required.append("aws")
    return required
```

Each binary is checked with `shutil.which`; a missing binary produces a problem like `missing required binary on PATH: 'ffmpeg'`.

### Required environment variables

`_required_env_vars(config)` returns the env-var names that must be set. Secrets are referenced by name only and resolved through `resolve_env` — the pre-flight check never reads secret values.

```python
def _required_env_vars(config: Config) -> list[str]:
    required = [config.telegram.bot_token_env]            # always, for dissemination

    if config.openrouter is not None:
        required.append(config.openrouter.api_key_env)    # always (R13/R16)

    if config.summary.backend == "agno" and config.notion.token_env:
        required.append(config.notion.token_env)          # iff summary=agno

    return required
```

The rules are:

- **Telegram bot token** — always required (the `bot_token_env` field).
- **OpenRouter API key** — unconditionally required. Transcription itself runs over the OpenRouter HTTP seam, so the key is required regardless of slides/summary backend selection.
- **Notion token** — required only when `summary.backend == "agno"` and `config.notion.token_env` is set. The `agy` backend publishes to Notion via its own MCP, so no engine-side token env is required for that path.

A missing variable produces a problem like `missing required environment variable: 'TG_TOKEN'`.

### Missing openrouter section fails fast

Because the `openrouter` block is now mandatory (transcription runs over its HTTP seam), a config lacking the section is caught explicitly before any binaries or env vars are checked:

```python
if config.openrouter is None:
    problems.append(
        "missing required 'openrouter' config section: transcription runs "
        "over the OpenRouter API, so the block (and its api_key_env) is "
        "required for every run"
    )
```

### The `check` subcommand

`transcriber check [--config PATH]` runs the pre-flight check standalone. `_cmd_check` formats the selected backends per stage (`describe_slides backend='openrouter'` or `describe_slides: disabled`, plus `summarize backend='<backend>'`) and exits 0 when all required binaries and env vars are present, non-zero (1) otherwise.

```
transcriber check --config config.yaml   # exit 0 on pass, 1 on fail
```

Exit codes for the whole CLI:

- **0** — all recordings succeeded, or no new recordings, or dry-run completed.
- **1** — pre-flight check failed, or any recording failed in the batch.
- **2** — config loading failed.

## Dry-run (P1)

`--dry-run` prints the execution plan and exits without performing any side effects: no subprocesses, no agent runs, no publishing, no deletion, and no manifests written.

```
transcriber --config config.yaml --dry-run
```

Dry-run does **not** enforce the pre-flight check. It discovers new recordings (recordings whose summary `.md` does not yet exist), builds the plan from `_enabled_stage_names(config)`, and prints it via `print_plan`.

### Execution plan output

`print_plan` emits:

- An `Execution plan (dry-run):` header.
- Per recording: `recording: <name>.mp4` and `stages: <comma-separated stage tokens>`.
- Publishing destinations: Notion server/parent page id, Telegram chats, and S3 bucket/profile when enabled (or `S3: disabled`).
- When there are no new recordings: `no new recordings found in <recordings_dir>`.

### Dry-run stage tokens

`_enabled_stage_names(config)` returns the ordered token list that appears in the plan. The tokens are pinned and disambiguated as follows:

| Token | Present when | Notes |
|---|---|---|
| `audio-extract` | always | pipeline audio extraction |
| `scene-extract` | `stages.slides.enabled` | pipeline slide/scene extraction — **not** `slides` |
| `transcribe [openrouter:<model_id>]` | always | surfaces the OpenRouter backend + configured model |
| `describe-slides [<backend>]` | `stages.slides.enabled` | paid slides backend call — only when slides on |
| `summarize+notion [<backend>]` | always | surfaces the selected summarize backend |
| `telegram` | always | dissemination |
| `s3-sync` | S3 sync enabled | gated S3 stage |

Ordering is fixed: `scene-extract` (when present) comes after `audio-extract` and before `transcribe`; `describe-slides` comes after `transcribe` and before `summarize+notion`.

#### Disambiguation: scene-extract vs describe-slides

These are distinct stages and must not be conflated in the plan:

- **`scene-extract`** — the deterministic pipeline stage that runs `scenedetect` to extract slide images. It is media-only and appears in the plan only when slides are enabled.
- **`describe-slides [<backend>]`** — the manifest-gated, paid/network stage that runs the configured slides backend (always `openrouter`) to produce `<name>.slides.md`. It appears only when slides are enabled.

Both appear in the plan only when `stages.slides.enabled` is true, and both are absent when slides are disabled.

#### Disambiguation: describe-slides vs summarize+notion

- **`describe-slides [<backend>]`** is conditional on slides being enabled. When slides are disabled the token is absent entirely.
- **`summarize+notion [<backend>]`** is always present in the plan, with the selected backend surfaced in the token (e.g. `summarize+notion [agy]` or `summarize+notion [agno]`).

#### Transcribe token

The transcribe token always surfaces the OpenRouter backend and the configured model, e.g. `transcribe [openrouter:microsoft/mai-transcribe-2]`.

### No side effects

During dry-run:

- No stage functions are invoked (the `stage_calls` list stays empty in tests).
- No `<name>.transcriber_state.json` manifests are written.
- The process exits 0 even when binaries or env vars are missing, because pre-flight is not enforced.

## Per-recording state manifest (E1)

Each recording gets a sidecar JSON manifest next to its `.mp4`:

```
<name>.transcriber_state.json
```

The manifest is the source of truth for stage completion — not artifact existence alone. It is read and written by `RecordingState` in `/transcriber/state.py`.

### The manifest tuple

The canonical ordered stage tuple tracked by the manifest is:

```python
STAGES: tuple[str, ...] = (
    "pipeline",
    "describe_slides",
    "summarize",
    "notion",
    "telegram",
    "s3",
    "cleanup",
)
```

The JSON document is small:

```json
{
  "name": "<name>",
  "completed": {
    "pipeline": true,
    "describe_slides": true,
    ...
  }
}
```

Only stages that are marked complete are stored as `true`. Any stage not present is treated as incomplete.

### RecordingState

`RecordingState(recording)` is a read/write wrapper over a single recording's manifest.

- **Construction** loads the existing manifest if present; a missing, corrupt, or unreadable manifest is treated as "nothing complete" (an empty `_completed` dict). Corrupt manifests are safe to re-run because individual stages are idempotent or gated by their own checks.
- **`is_complete(stage)`** returns `self._completed.get(stage, False)`. The `.get(stage, False)` semantics are deliberate: an absent key means incomplete.
- **`mark_complete(stage)`** validates that `stage` is in `STAGES`, sets `_completed[stage] = True`, and persists immediately via `save()`. Persisting after each stage means a crash between stages leaves an accurate manifest for the next retry.
- **`save()`** writes the payload atomically-ish to disk.
- **`delete()`** removes the manifest file — called after a recording's full success.

### Pre-describe_slides manifests are treated as incomplete

Manifests written before `describe_slides` existed as a tracked stage simply lack that key. Because completion is read as `_completed.get(stage, False)`, an absent `describe_slides` entry is treated as **incomplete**. Re-running a recording whose manifest predates this stage re-issues the slides backend call exactly once, records it, and makes further re-runs idempotent.

This is the same `.get(stage, False)` contract that governs every stage: absence is incompleteness, not a skip signal.

## Manifest-gated stage skipping

The orchestrator `_process_one` runs each stage in order and consults the manifest before acting. The skip rules per stage are:

### 1. Pipeline

```
if state.is_complete("pipeline"):
    skip
else:
    pipeline_mod.process_recording(mp4, config)
    state.mark_complete("pipeline")
```

### 2. Describe slides

```
if not config.stages.slides.enabled:
    skip (disabled)
elif state.is_complete("describe_slides"):
    skip (already complete)
elif slides_md_path.exists():
    idempotent skip — artifact present, do NOT re-issue paid call
    state.mark_complete("describe_slides")
else:
    run backend.describe(...)
    write <name>.slides.md
    state.mark_complete("describe_slides")
```

The idempotent skip when `<name>.slides.md` already exists handles the case where a prior run wrote the artifact but crashed before marking the stage complete. The paid backend call is not re-issued; the stage is marked complete so a subsequent re-run skips it normally.

When slides are disabled, the stage is skipped entirely and never writes a slides markdown file.

### 3. Summarize + Notion

```
if state.is_complete("summarize") and state.is_complete("notion"):
    skip
else:
    slides_markdown = None if slides disabled or slides.md absent else read
    summarize_backend.summarize(transcript_path, slides_markdown, recording_dir, config)
    state.mark_complete("summarize")
    state.mark_complete("notion")
```

`summarize` and `notion` are tracked separately but marked complete together, because both backends publish to Notion as part of the same run. A Notion failure fails the whole `summarize`+`notion` stage.

When slides are disabled, a stale `<name>.slides.md` is never fed into the summary — the summarize backend receives `None`, exactly as if slides were off.

### 4. Telegram

```
if state.is_complete("telegram"):
    skip
else:
    send digest (or full summary fallback)
    state.mark_complete("telegram")
```

### 5. S3 sync

```
if not s3_mod.is_enabled(config):
    skip (disabled)
elif state.is_complete("s3"):
    skip (already complete)
else:
    s3_mod.sync(recording_dir, config)
    state.mark_complete("s3")
```

S3 sync is gated: it is a no-op when `config.stages.s3_sync` is falsy or `config.s3` is `None`.

### 6. Cleanup

```
if keep_intermediates:
    skip (keep-intermediates/debug)
elif state.is_complete("cleanup"):
    skip (already complete)
else:
    cleanup_mod.cleanup(mp4, keep_intermediates=keep_intermediates)
    state.mark_complete("cleanup")
    state.delete()
```

Cleanup runs only on full success, and only when intermediates are not being kept. On full success the manifest is deleted.

## Failure isolation

`_process_one` wraps all stages in a single try/except. A raised exception in one recording is caught, logged with the failing stage name, and returned as a failed `RecordingOutcome`. The batch continues to the next recording.

```python
except Exception as exc:
    logger.exception("[%s] failed at stage '%s': %s", name, current_stage, exc)
    return RecordingOutcome(name=name, succeeded=False, failed_stage=current_stage, error=str(exc))
```

The consequences for a failed recording:

- The manifest records the stages that completed before the failure. For example, if the summarize backend raises, the manifest records `pipeline` complete but **not** `describe_slides` (if it ran), `summarize`, `notion`, `telegram`, `s3`, or `cleanup`.
- No `telegram` send occurs for the failed recording.
- No `cleanup` runs for the failed recording (cleanup is only reached on the happy path).
- The batch exit code is 1 if any recording failed, even though the batch ran to completion.

`main` returns 0 when all outcomes succeeded, 1 when any failed, and 2 only on config-load failure.

## Re-run skip and fail paths

The orchestrator re-run behavior splits on whether a recording previously succeeded, partially failed, or is fresh. The diagram below shows the skip/fail decision paths the manifest drives.

<!-- openwiki: mermaid parse failed and this diagram was converted to a text fence so it does not break rendering. Fix the diagram source and restore the mermaid fence. Parser error: Heuristic: an unescaped angle bracket inside a label breaks rendering; rephrase the label. -->
```text
flowchart TD
    Start[Recording picked up by discovery] --> LoadState[Load RecordingState]
    LoadState --> CheckPipeline{is_complete<br/>pipeline?}
    CheckPipeline -->|yes| SkipPipeline[Skip pipeline]
    CheckPipeline -->|no| RunPipeline[process_recording<br/>mark_complete pipeline]
    SkipPipeline --> CheckSlides
    RunPipeline --> CheckSlides{ Slides enabled<br/>and<br/>not complete? }
    CheckSlides -->|no| SlidesSkip[Skip or mark_complete<br/>describe_slides]
    CheckSlides -->|yes| SlidesArtifact{ slides.md<br/>already exists? }
    SlidesArtifact -->|yes| SlidesIdempotent[mark_complete<br/>describe_slides<br/>no paid call]
    SlidesArtifact -->|no| SlidesRun[backend.describe<br/>write slides.md<br/>mark_complete]
    SlidesSkip --> CheckSummarize
    SlidesIdempotent --> CheckSummarize
    SlidesRun --> CheckSummarize{ is_complete<br/>summarize<br/>and notion? }
    CheckSummarize -->|yes| SkipSummarize[Skip summarize + notion]
    CheckSummarize -->|no| RunSummarize[backend.summarize<br/>mark_complete summarize<br/>mark_complete notion]
    SkipSummarize --> CheckTelegram
    RunSummarize --> CheckTelegram{ is_complete<br/>telegram? }
    CheckTelegram -->|yes| SkipTelegram[Skip telegram]
    CheckTelegram -->|no| RunTelegram[send digest/summary<br/>mark_complete telegram]
    SkipTelegram --> CheckS3
    RunTelegram --> CheckS3{ S3 enabled<br/>and not complete? }
    CheckS3 -->|no| SkipS3[Skip or mark_complete s3]
    CheckS3 -->|yes| RunS3[s3.sync<br/>mark_complete s3]
    SkipS3 --> CheckCleanup
    RunS3 --> CheckCleanup{ keep_intermediates<br/>or complete? }
    CheckCleanup -->|yes| SkipCleanup[Skip cleanup]
    CheckCleanup -->|no| RunCleanup[cleanup<br/>mark_complete cleanup<br/>state.delete]
    SkipCleanup --> Success[Recording success<br/>exit path]
    RunCleanup --> Success
    RunS3 --> CheckCleanup
    RunTelegram --> CheckS3
    SkipSummarize --> CheckTelegram
    SlidesRun --> CheckSummarize
```

<!-- openwiki: mermaid parse failed and this diagram was converted to a text fence so it does not break rendering. Fix the diagram source and restore the mermaid fence. Parser error: Heuristic: an unescaped angle bracket inside a label breaks rendering; rephrase the label. -->
```text
flowchart TD
    FailStart[Stage raises exception] --> LogFail[logger.exception<br/>record current_stage]
    LogFail --> ReturnFail[RecordingOutcome<br/>succeeded=False<br/>failed_stage=current_stage]
    ReturnFail --> BatchContinues[Batch continues to next recording]
    BatchContinues --> BatchSummary[Exit 1 if any failed<br/>exit 0 if all succeeded]
    ReturnFail --> ManifestPartial[Manifest records<br/>only stages completed<br/>before failure]
    ManifestPartial --> ReRun[Re-run: completed stages<br/>skipped, failed stage<br/>re-run once]
```

The failure path is isolated per recording: the failed recording's manifest ends up partially complete, cleanup is skipped for that recording, and a subsequent re-run picks up from the last completed stage. The happy path deletes the manifest on full success, so a fresh re-discovery treats the recording as new again only if the summary `.md` is absent.

## Config.debug and keep-intermediates

Two mechanisms keep intermediate artifacts after a successful run:

- **`--keep-intermediates`** CLI flag.
- **`debug: true`** in the config file.

They combine as:

```python
keep_intermediates = args.keep_intermediates or config.debug
```

When `keep_intermediates` is true, the cleanup stage is skipped for every recording (the manifest is still marked and the recording is treated as successful). `debug: true` is persistent across runs, which is useful while the tool matures; `--keep-intermediates` is a one-off override.

When intermediates are kept, the cleanup stage logs `cleanup: skipped (keep-intermediates/debug)` and the manifest still records the run as successful.
