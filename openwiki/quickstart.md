---
type: guide
title: Quickstart — Run the Transcriber Engine
description: Install dependencies, configure a host, and run check, dry-run, and batch transcription with the transcriber engine.
tags: [transcriber, quickstart, setup, config, cli]
verified:
  - by: openwiki/0.9.0
    at: 2026-11-07T16:24:10.535Z
sources:
  - id: openwiki-source-93673910bb15c021d9d7054e
    resource: repo://examples/example.config.yaml
  - id: openwiki-source-05ccef8d4cf1698187f20464
    resource: repo://pyproject.toml
  - id: openwiki-source-23775c3de52f3ab95a13cb8b
    resource: repo://README.md
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
  - id: openwiki-source-00e3ed9e027733aaa6655e18
    resource: repo://transcriber/publish/telegram.py
  - id: openwiki-source-0c6dbb86c6b6001bf65f1b4c
    resource: repo://transcriber/state.py
generated: { by: "openwiki/0.9.0", at: "2026-11-07T16:24:10.535Z" }
---

# Quickstart — Run the Transcriber Engine

This wiki documents a **local-first, AI-enabled meeting transcriber engine**. The engine code stays identical across hosts; each host supplies only its own config file, environment-variable secrets, and a `recordings/` directory. Hosts consume it as an **installable git-ref dependency** exposing the `transcriber` console entry point — copy an example config into the host repo and run from the host root.

Use this page to install, configure, and run your first batch, then follow the links into the domains that matter for your task.

## Where to go next

- **Architecture** — how the batch run composes. See [Architecture: OSS companion transcriber engine](architecture/oss-companion-transcriber.md).
- **Concepts** — the validated config model and per-stage backend selectors. See [Configuration model](concepts/config-model.md) and [Backend selection](concepts/backend-selection.md).
- **Workflows** — the concrete stage behavior. See [Transcription pipeline](workflows/transcription-pipeline.md), [Slide description](workflows/slides-description.md), and [Summarize, digest, routing, and Notion publish](workflows/summarize-publish.md).
- **Integrations** — the external seams. See [OpenRouter](integrations/openrouter.md), [Notion](integrations/notion.md), [Telegram](integrations/telegram.md), and [S3](integrations/s3.md).
- **Operations** — pre-flight, manifests, failures, and cleanup. See [Pre-flight, manifests, failures, and cleanup](operations/pre-flight-and-idempotency.md).
- **Testing** — how the engine is tested. See [Testing strategy](testing/strategy.md).

If you only need the minimal setup flow, keep reading.

## Install

Requirements: **Python 3.10+** and [uv](https://docs.astral.sh/uv/) for environment management.

```bash
uv sync
```

`agno` is a **core dependency** (not an optional extra): `pyproject.toml` depends on `agno[mcp]~=3.0` plus `openai~=1.0` because the `summarize` stage always runs on the backend named `agno`. The deprecated no-op `transcriber[agno]` extra still resolves for old host dependency strings, but new hosts depend on plain `transcriber`. The `transcriber` console script is mapped to `transcriber.__main__:main`.

Host `pyproject.toml` pattern:

```toml
[project]
dependencies = ["transcriber"]

[tool.uv.sources]
transcriber = { git = "ssh://git@github.com/scartill/transcriber.git", tag = "vX.Y.Z" }
```

## Configure a host

A single config file (YAML or JSON, selected by extension) loads into one validated `Config` model in `transcriber/config.py`. Secrets are never stored in the file — they are referenced by **environment-variable name only** and resolved at use time via `resolve_env`.

Start from an example:

- `examples/example.config.yaml` — the simpler setup: slides on, no S3 sync, English summaries, a single Telegram chat, Notion subpage insertion, slides on the OpenRouter vision backend, summarize on the `agno` backend (the name is retained for compatibility; the implementation is direct OpenRouter chat-completions calls plus engine-driven Notion REST publish).
- `examples/acme.config.yaml` — the fuller setup: S3 sync on, original-language summaries, topic-routed Telegram.

At a high level the config separates concerns:

- **Where recordings live** — `recordings_dir`.
- **Which stages run** — `stages.slides` (`{enabled, backend}`; the legacy bare-bool form raises `ConfigError`) and `stages.s3_sync`.
- **How audio is transcribed** — `transcribe` (OpenRouter STT slug, diarization, `segment_seconds` / `overlap_seconds`).
- **How the summary is produced** — `summary` (`language`, `sections`, `backend`), plus `agent.output_file` (`{basename}.md` template).
- **How external services connect** — a mandatory `openrouter` block and optional `s3`; `notion` and `telegram` connection blocks.
- **Timeouts and debug behavior** — `timeouts` and optional top-level `debug`.

Rules that matter early:

- The `openrouter` block is **always required**. Transcription runs over the OpenRouter speech-to-text API on every run, so there is no OpenRouter-free configuration.
- `stages.slides.backend` must be `"openrouter"` and `summary.backend` must be `"agno"` — the only values in each selector. Unknown values fail at load.
- `notion.token_env` is required when `summary.backend == "agno"`.
- `telegram.enabled` defaults to `True`. When `False`, the Telegram stage is skipped entirely.

### Config discovery

`transcriber` resolves its config in strict precedence:

1. explicit `--config PATH`;
2. `TRANSCRIBER_CONFIG` when set and non-empty (empty/whitespace counts as unset);
3. first existing CWD candidate — `transcriber.config.yaml`, then `config.yaml` (the former wins with a shadow notice when both exist);
4. otherwise raise `ConfigNotFoundError` naming all four sources.

An explicit `--config` and a non-empty `TRANSCRIBER_CONFIG` are **authoritative**: the path must be an existing regular file (a missing path or directory is a hard error with no fallback to CWD candidates). Relative `--config` / `TRANSCRIBER_CONFIG` paths are anchored to the current working directory, so run `transcriber` from the host root. The resolved path is logged at `INFO` on every run and shown in `check` / `--dry-run` output.

## Check, plan, run

The CLI surface is small:

```bash
transcriber [--config PATH] [--keep-intermediates] [--dry-run]
transcriber check [--config PATH]
```

- `--config PATH` selects the config file (see discovery above).
- `transcriber check` runs the **pre-flight check standalone**: required binaries on `PATH` plus required env-var names, with the selected backend per stage surfaced. It exits `0` when everything is present and non-zero with a message otherwise.
- `transcriber --dry-run` prints the **execution plan** and exits with no side effects — no subprocesses, no model calls, no publishing, no deletion.
- `--keep-intermediates` skips the cleanup stage after a successful run. The config `debug: true` flag has the same effect persistently; the two are OR'd.

Before any real work, the engine fails fast on pre-flight problems:

- **Binaries:** `ffmpeg` is always required; `scenedetect` is additionally required when slides are enabled; `aws` is additionally required when S3 sync is enabled. `agno` is a Python dependency installed by `uv sync`, not a checked binary.
- **Env vars (by name only, values never read here):** the OpenRouter API-key variable is required on every run; the Telegram bot-token variable is required only when `telegram.enabled` is true; the Notion token variable is additionally required when `summary.backend == "agno"`.

Both `check` and `--dry-run` surface **backend visibility** per stage so you can confirm the engine mix before it executes. A slides-on, `agno`-summarize plan reads:

```
audio-extract, scene-extract, transcribe [openrouter:microsoft/mai-transcribe-2], describe-slides [openrouter], summarize+notion [agno], telegram, s3-sync
```

### A minimal first run

```bash
cp examples/example.config.yaml ./transcriber.config.yaml
# edit recordings_dir, notion.parent_page_id, telegram ids, s3.* as needed

export TELEGRAM_BOT_TOKEN=...
export OPENROUTER_API_KEY=...   # always required
export NOTION_API_KEY=...       # required for summarize backend "agno"

transcriber check --config transcriber.config.yaml
transcriber --config transcriber.config.yaml --dry-run
mkdir -p recordings
# drop a new *.mp4 into recordings/, then:
transcriber --config transcriber.config.yaml
```

A recording is **new** when its `*.mp4` has no corresponding summary `.md` yet. Discovery is sorted by filename for deterministic ordering.

## What a batch does

Each new `*.mp4` runs through a fixed sequence:

1. **Pipeline** — deterministic, media-only stages: `ffmpeg` audio extract, optional `scenedetect` scene extraction, then chunked OpenRouter speech-to-text transcribe and stitching.
2. **Describe slides** *(only when `stages.slides.enabled`)* — one OpenRouter vision call per slide, image-only, writing `<name>.slides.md` via the `openrouter` backend.
3. **Summarize + Notion** — the backend named `agno` makes direct OpenRouter chat-completions calls for the summary and digest, writes `<name>.md` plus `<name>.telegram.md` (and `<name>.slides-clean.md` when slides exist) **before** publishing, then the engine publishes the Notion subpage via the Notion REST API with a duplicate-publish guard.
4. **Telegram** *(only when `telegram.enabled`)* — disseminate the digest, topic-routed where configured.
5. **S3** *(only when `stages.s3_sync`)* — sync the recording to S3.
6. **Cleanup** — remove intermediate artifacts after a successful run (unless `--keep-intermediates` or `debug: true`).

The durable outputs are preserved: the source `.mp4`, the final `.md` summary, the `.txt` transcript, and the reader-facing `<name>.slides-clean.md`.

## Idempotency and failure handling

Retries are safe by design. The engine keeps a **per-recording state manifest** (`<name>.transcriber_state.json`) next to each `.mp4`, and a retry skips already-finished stages — this is what prevents duplicate Notion pages, duplicate Telegram sends, or redundant S3 re-syncs.

- The manifest is the source of truth for stage completion.
- `describe_slides` has an extra idempotent path: if the manifest is incomplete but `<name>.slides.md` already exists, the paid backend call is skipped and the stage is marked complete.
- Failure is isolated per recording: an exception is caught, logged, recorded in that recording's manifest, and the batch continues. Cleanup runs only on a recording's full success.
- There is **no silent cross-backend fallback**: a selected backend's failure fails that stage and is retryable.

If you are changing the engine, start with [Architecture](architecture/oss-companion-transcriber.md) and the workflow pages matching the stage you touch. If you are operating a host repo, start with [Pre-flight, manifests, failures, and cleanup](operations/pre-flight-and-idempotency.md) and the integration pages for the services you use.
