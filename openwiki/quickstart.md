---
type: "Reference"
title: "Quickstart"
openwiki_generated: true
verified:
  - by: openwiki/0.6.1
    at: 2026-10-02T19:10:14.922Z
sources:
  - id: openwiki-source-93673910bb15c021d9d7054e
    resource: repo://examples/example.config.yaml
  - id: openwiki-source-df4110b2c5338913ae9eedcf
    resource: repo://transcriber/__main__.py
  - id: openwiki-source-bb626cf9b19692a8c8d8d744
    resource: repo://transcriber/agent.py
  - id: openwiki-source-8a3a341e370fcf0ec5b903de
    resource: repo://transcriber/backends/interfaces.py
  - id: openwiki-source-d03ac1f85a9e3bd2b413eec1
    resource: repo://transcriber/backends/slides.py
  - id: openwiki-source-8ae57789b707a6fc3e3769d7
    resource: repo://transcriber/backends/summarize_agno.py
  - id: openwiki-source-5670223669d02f5c4fdf5d64
    resource: repo://transcriber/backends/summarize.py
  - id: openwiki-source-005cc63fa54282b91136f803
    resource: repo://transcriber/backends/transcribe_openrouter.py
  - id: openwiki-source-c4777b8db8d4806695ac8b6a
    resource: repo://transcriber/config.py
  - id: openwiki-source-c09b28db65820f5184d0fc9f
    resource: repo://transcriber/pipeline.py
  - id: openwiki-source-0c6dbb86c6b6001bf65f1b4c
    resource: repo://transcriber/state.py
generated: { by: "openwiki/0.6.1", at: "2026-10-02T19:10:14.922Z" }
---


# Quickstart

This wiki documents a **local-first, AI-enabled meeting transcriber engine** that is designed to be embedded in another repository as a **git submodule**. The engine code stays identical across hosts; each host supplies its own config file, environment-variable secrets, and a `recordings/` directory.

Use this page to get oriented, then follow the links into the domains that matter for your task.

## Where to go next

The wiki is organized around a few major domains. Pick the one that matches what you are trying to do:

<!-- openwiki: broken internal link [../architecture/oss-companion-transcriber.md] file "../architecture/oss-companion-transcriber.md" does not exist. Fix the href or restore the target, then delete this comment. -->
- **Architecture** — how the owned systems compose into one batch run: config loading, the pipeline stage sequence, the pluggable post-transcript backends, Notion publishing, Telegram dissemination, S3 sync, and the orchestrator with manifest-based idempotency. See [Architecture: OSS companion transcriber engine](../architecture/oss-companion-transcriber.md).
<!-- openwiki: broken internal link [../concepts/config-model.md] file "../concepts/config-model.md" does not exist. Fix the href or restore the target, then delete this comment. -->
<!-- openwiki: broken internal link [../concepts/backend-selection.md] file "../concepts/backend-selection.md" does not exist. Fix the href or restore the target, then delete this comment. -->
- **Concepts** — the configuration model and how backends are selected. See [Configuration model](../concepts/config-model.md) and [Backend selection and interfaces](../concepts/backend-selection.md).
<!-- openwiki: broken internal link [../workflows/transcription-pipeline.md] file "../workflows/transcription-pipeline.md" does not exist. Fix the href or restore the target, then delete this comment. -->
<!-- openwiki: broken internal link [../workflows/slides-description.md] file "../workflows/slides-description.md" does not exist. Fix the href or restore the target, then delete this comment. -->
<!-- openwiki: broken internal link [../workflows/summarize-publish.md] file "../workflows/summarize-publish.md" does not exist. Fix the href or restore the target, then delete this comment. -->
- **Workflows** — the concrete stage-by-stage behavior. See [Transcription pipeline workflow](../workflows/transcription-pipeline.md), [Slide description workflow](../workflows/slides-description.md), and [Summarize and publishing workflow](../workflows/summarize-publish.md).
<!-- openwiki: broken internal link [../integrations/openrouter.md] file "../integrations/openrouter.md" does not exist. Fix the href or restore the target, then delete this comment. -->
<!-- openwiki: broken internal link [../integrations/notion.md] file "../integrations/notion.md" does not exist. Fix the href or restore the target, then delete this comment. -->
<!-- openwiki: broken internal link [../integrations/telegram.md] file "../integrations/telegram.md" does not exist. Fix the href or restore the target, then delete this comment. -->
<!-- openwiki: broken internal link [../integrations/s3.md] file "../integrations/s3.md" does not exist. Fix the href or restore the target, then delete this comment. -->
- **Integrations** — the external seams: OpenRouter, Notion, Telegram, and S3. See [OpenRouter integration](../integrations/openrouter.md), [Notion integration](../integrations/notion.md), [Telegram integration](../integrations/telegram.md), and [S3 integration](../integrations/s3.md).
<!-- openwiki: broken internal link [../operations/pre-flight-and-idempotency.md] file "../operations/pre-flight-and-idempotency.md" does not exist. Fix the href or restore the target, then delete this comment. -->
- **Operations** — pre-flight checks, dry-run planning, and idempotent retries. See [Pre-flight checks and idempotent retries](../operations/pre-flight-and-idempotency.md).
<!-- openwiki: broken internal link [../testing/strategy.md] file "../testing/strategy.md" does not exist. Fix the href or restore the target, then delete this comment. -->
- **Testing** — the pytest approach for the orchestrator and CLI. See [Testing strategy](../testing/strategy.md).

If you only need the minimal setup flow, keep reading.

## Minimal setup flow

1. **Add the engine as a submodule** and keep host-specific customizations in config. The engine code itself does not change between hosts.
2. **Provide a config file** (YAML or JSON) plus a `recordings/` directory. Secrets are never stored in the config; they are referenced by environment-variable name and resolved at use time.
3. **Satisfy the prerequisites** before running: the required binaries must be on `PATH` and the required environment variables must be set. The engine ships a `transcriber check` pre-flight subcommand for this.
4. **Understand the stage sequence** so you know what "done" means for a recording and what can be skipped on retry.
5. **Pick the post-transcript backends** you want, because they determine which paid/network calls happen and which secrets are required.

## Config shape

<!-- openwiki: broken internal link [../concepts/config-model.md] file "../concepts/config-model.md" does not exist. Fix the href or restore the target, then delete this comment. -->
The config is loaded by a single validated model regardless of file format. Both JSON and YAML map to the same `Config` dataclass. The loader is in `transcriber/config.py` and is covered in detail in [Configuration model](../concepts/config-model.md).

At a high level, the config separates concerns like this:

- **Where recordings live** — `recordings_dir`.
- **Which stages run** — `stages.slides` (a nested `{enabled, backend}` object; the legacy bare-bool form is rejected) and `stages.s3_sync`.
- **How audio is transcribed** — `transcribe`, including the OpenRouter STT model slug, diarization toggle, and chunking/overlap parameters.
- **How the summary is produced and published** — `summary` (language, sections, backend), `agent` (the local CLI settings used by the `agy` path), `notion`, and `telegram`.
- **How external services connect** — a shared `openrouter` block (now mandatory) and optional `s3`.
- **Timeouts and debug behavior** — `timeouts` and the optional top-level `debug` flag.

The example config is a useful starting point: `examples/example.config.yaml`. It is the simpler "example" host setup: slides on, no S3 sync, English summaries, a single Telegram chat, and a Notion subpage insertion mode.

A few config rules matter early:

- The `openrouter` block is **always required**. Transcription itself runs over the OpenRouter speech-to-text API, so audio egresses to OpenRouter on every run. There is no OpenRouter-free configuration.
- Secrets are stored as **environment-variable names only**, never as literal values. For example, `telegram.bot_token_env` names the variable that holds the token; the value is read later via `resolve_env`.
- `stages.slides` is a nested mapping with `{enabled, backend}`. The old bare-boolean shape is invalid and raises a `ConfigError`.
- The slides backend is constrained to `"openrouter"`, and the summarize backend is constrained to `"agy"` or `"agno"`. Unknown values fail at load.

## Prerequisites

The engine is a Python CLI. Before any real work happens, it runs a pre-flight check that verifies the binaries and environment variables required by the current config.

The always-required binary is `ffmpeg`. Other binaries are gated by config:

- `scenedetect` when slides are enabled.
- `agy` when the summarize backend is `agy`.
- `aws` when S3 sync is enabled.

On the environment-variable side, the pre-flight check treats secrets by name only. The Telegram bot token is always required for dissemination. The OpenRouter API-key variable is required on every run because transcription runs over OpenRouter. The Notion token variable is additionally required when `summary.backend == "agno"`.

You can run the pre-flight check directly:

```bash
transcriber check --config config.yaml
```

It exits `0` when everything required is present and non-zero with a message otherwise. The `--dry-run` flag is also useful early: it prints the execution plan for the current config and recordings and exits without running any subprocesses, agent, publishing, or deletion.

## Pipeline stage sequence

Each new `*.mp4` in `recordings_dir` (one whose summary `.md` does not yet exist) runs through a fixed sequence. The high-level order is:

1. **Pipeline** — deterministic, media-only stages:
   - **Audio extract** — pull an audio track from the video with `ffmpeg`.
   - **Scene extraction** *(optional, `stages.slides.enabled`)* — detect scene cuts and export slide frames with `scenedetect`. This stage is network-free.
   - **Transcribe** — diarize and chunk the audio into explicit time slices, send each slice to **OpenRouter** speech-to-text, then stitch the slices back into a single plain-text transcript.
2. **Describe slides** *(optional, when `stages.slides.enabled`)* — turn the extracted slide images into a `<name>.slides.md` markdown block via the **OpenRouter vision backend**. One vision call per slide, image-only (no transcript).
3. **Summarize** — turn the transcript (plus the `<name>.slides.md` block when present) into the final `<name>.md` summary and `<name>.telegram.md` digest, and publish a Notion subpage. This runs on the configured **summarize backend**.
4. **Telegram** — disseminate a notification to Telegram, topic-routed where configured.
5. **S3** *(optional, when `stages.s3_sync`)* — sync the recording to S3.
6. **Cleanup** — remove intermediate artifacts after a successful run (unless `--keep-intermediates` or `debug: true`).

Intermediate artifacts are cleaned up when a recording finishes processing. The durable outputs are preserved: the source `.mp4`, the final `.md` summary, the `.txt` transcript, and the reader-facing `<name>.slides-clean.md`.

The pipeline stage itself stays network-free. The paid/network calls live in the manifest-gated post-transcript steps: describe slides, summarize/+Notion, Telegram, and S3.

## Idempotency and retry behavior

Retries are safe by design. The engine keeps a **per-recording state manifest** (`<name>.transcriber_state.json`) next to each `.mp4`. That manifest records which stages have completed, and a retry skips already-finished stages. This is what prevents duplicate Notion pages, duplicate Telegram sends, or a redundant S3 re-sync when a batch is re-run after a partial failure.

A few important details:

- The manifest — not artifact existence alone — is the source of truth for stage completion.
- Already-completed stages are skipped. For example, if `pipeline` is already marked complete, the orchestrator logs that and moves on.
- The `describe_slides` stage has an extra idempotent path: if the manifest says the stage is not complete but `<name>.slides.md` is already present, the paid backend call is skipped and the stage is marked complete. This handles the crash-after-write-but-before-mark case without reissuing the call.
- If a manifest predates the `describe_slides` stage, the missing key is treated as incomplete, so a re-run reissues the slides call exactly once and then records it.
- Failure is isolated per recording. An exception in one recording is caught, logged, recorded in that recording's manifest, and the batch continues. Cleanup runs only on a recording's full success.

Because paid calls live in manifest-gated orchestrator steps, a retry never re-issues a completed slides call. There is **no silent cross-backend fallback**: a selected backend's failure fails that stage (and is retryable).

## The two post-transcript backend choices

Two post-transcript stages matter most when you are setting up a host: **describe slides** and **summarize**. They are selected independently.

### Describe slides

The `describe_slides` stage always runs on the **`openrouter`** vision backend. It is the only slides backend. It performs one OpenRouter vision call per slide, image-only (no transcript). Enabling slides requires the `openrouter` block and its API key.

The slide descriptor is image-only: it describes what is visually on each slide. Transcript cross-referencing — aligning speaker commentary, decisions, and Q&A to slides — happens later in the summarize stage, which receives the full transcript **and** the `<name>.slides.md` block.

Slides are gated by `stages.slides.enabled`. If you must avoid all OpenRouter egress, keep slides disabled and summarize on `agy`.

### Summarize

The `summarize` stage runs on a separately selected backend:

- **`agy`** — the local CLI agent. This is the default when nothing is configured. Notion is published via `agy`'s own MCP as part of its run.
- **`agno`** — an OpenRouter-driven summarize backend. The model produces the summary and digest, and then **the engine publishes the Notion subpage directly via the Notion REST API** (no MCP, no `npx`).

Both backends produce the same artifacts, but the Notion publish path is backend-specific. For `agy`, Notion publishing is agent-driven. For `agno`, it is engine-driven through the Notion REST API.

Summarize defaults to `agy`. If you enable slides, you must also have the `openrouter` block and key, because the slide descriptor is an OpenRouter vision call. Operators who must avoid all OpenRouter egress should keep slides disabled and summarize on `agy`.

## A minimal first run

A minimal happy path looks like this:

1. Copy `examples/example.config.yaml` into the host repo and adjust paths/ids.
2. Set the referenced environment variables (for example, `TELEGRAM_BOT_TOKEN` and `OPENROUTER_API_KEY`).
3. Run `transcriber check --config config.yaml` to verify binaries and environment.
4. Drop a new `*.mp4` into `recordings/` and run `transcriber --config config.yaml`.

If you want to preview what will happen before running anything, use:

```bash
transcriber --config config.yaml --dry-run
```

## How this wiki is organized

<!-- openwiki: broken internal link [../architecture/oss-companion-transcriber.md] file "../architecture/oss-companion-transcriber.md" does not exist. Fix the href or restore the target, then delete this comment. -->
<!-- openwiki: broken internal link [../operations/pre-flight-and-idempotency.md] file "../operations/pre-flight-and-idempotency.md" does not exist. Fix the href or restore the target, then delete this comment. -->
This page is intentionally short and concrete. Deep runtime analysis lives in the architecture and workflow pages. If you are changing the engine, start with [Architecture: OSS companion transcriber engine](../architecture/oss-companion-transcriber.md) and then read the workflow pages that match the stage you are touching. If you are operating a host repo, start with [Pre-flight checks and idempotent retries](../operations/pre-flight-and-idempotency.md) and the integration pages for whichever external services you use.
