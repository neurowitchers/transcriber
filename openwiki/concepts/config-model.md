---
type: concept
title: Configuration model
description: The transcriber's validated Config dataclass loaded from JSON or YAML, nested stages.slides, the mandatory openrouter block, backend selectors, Notion/Telegram/S3 fields, per-stage timeouts, and the secrets-by-env-name rule resolved at use time.
tags: [transcriber, config, dataclass, openrouter, yaml, json, secrets, validation, timeouts, notion, telegram, s3]
sources:
  - id: openwiki-source-6c4e8e364706f83ed08cc3c9
    resource: repo://examples/acme.config.yaml
  - id: openwiki-source-93673910bb15c021d9d7054e
    resource: repo://examples/example.config.yaml
  - id: openwiki-source-81af13fa7982f0b3becf1286
    resource: repo://tests/test_config.py
  - id: openwiki-source-c4777b8db8d4806695ac8b6a
    resource: repo://transcriber/config.py
generated: { by: "openwiki/0.6.1", at: "2026-10-06T06:48:26.024Z" }
verified:
  - by: openwiki/0.6.1
    at: 2026-10-06T06:48:26.024Z
---

# Configuration model

<!-- openwiki: broken internal link [../transcriber/config.py] file "../transcriber/config.py" does not exist. Fix the href or restore the target, then delete this comment. -->
Configuration is loaded in [transcriber/config.py](../transcriber/config.py) into a single validated dataclass model. Both `.json` and `.yaml`/`.yml` map to the identical `Config` model, selected by file extension at load time.

<!-- openwiki: broken internal link [../transcriber/config.py#resolve-env] file "../transcriber/config.py" does not exist. Fix the href or restore the target, then delete this comment. -->
Secrets are **never stored in the model**. The config references secrets by environment-variable **name only** (for example `telegram.bot_token_env` or `openrouter.api_key_env`); the actual value is resolved at *use* time via [resolve_env](../transcriber/config.py#resolve-env). That means config files contain no inline credentials, and S3 auth uses a named AWS profile rather than embedded keys.

## Dataclass model

The top-level model is:

- `recordings_dir` — path to the directory scanned for `.mp4` recordings.
- `stages` — the `Stages` dataclass (`slides: SlidesStage`, `s3_sync: bool`).
- `transcribe` — `Transcribe(model_id, diarize, segment_seconds, overlap_seconds)`.
- `summary` — `Summary(language, sections, backend)`.
- `agent` — `Agent(cli, extra_args, output_file)`.
- `notion` — `Notion(server, parent_page_id, insert, token_env)`.
- `telegram` — `Telegram(bot_token_env, default_chat_id, routing)`.
- `timeouts` — `Timeouts(...)` per-stage timeout block.
- `s3` — optional `S3(bucket, profile)`.
- `openrouter` — **optional in the dataclass declaration but required at load** (`api_key_env`, `base_url`, `slides_model`, `summary_model`, `max_slides`).
- `debug` — optional boolean, defaults `False`, keeps intermediate artifacts after a successful run.

## Nested stages.slides

`Stages.slides` is now a nested dataclass (`SlidesStage`) with `enabled: bool` and `backend: str` ("openrouter"). The legacy bare-boolean form (`slides: true`) is **not accepted** and raises `ConfigError` at load. Slides backend selection is constrained to `"openrouter"` (the only slides backend), validated in `_from_dict` against `SLIDES_BACKENDS`.

## Mandatory openrouter block

The `openrouter` block is now **unconditionally required** at config load. It is the transcription backend (OpenRouter speech-to-text) in addition to the existing slides and `agno` triggers, so every config without an `openrouter` section raises `ConfigError`. Representative validation:

- missing `openrouter` raises `"'openrouter' section is required: it is the transcription backend (OpenRouter STT), and is also required when slides are enabled (stages.slides.enabled) or summary.backend == 'agno'"`.

The shared `openrouter` block carries **one block, two per-stage models**:

- `slides_model` — used by the `openrouter` slides backend (default `google/gemini-2.0-flash-001`).
- `summary_model` — used by the `agno` summarize backend (default `google/gemini-2.5-pro`).

## Backend selectors

Two stage backends are selected as pure functions of config and validated at load:

- `describe_slides` → `config.stages.slides.backend`, only `"openrouter"`.
- `summarize` → `config.summary.backend`, `"agno"` (the only summarize backend, and the default).

`summary.language` must be `"en"` or `"original"`; invalid values raise `ConfigError`.

Notion token requirement is backend-specific:

- `notion.token_env` is required when `summary.backend == "agno"` (engine publishes via Notion REST).

## Notion / Telegram / S3 fields

- `Notion` — `server`, `parent_page_id`, `insert`, and optional `token_env` (env-var name).
- `Telegram` — `bot_token_env`, `default_chat_id`, `routing` (topic → chat id mapping).
- `S3` — optional `bucket` + `profile` (named AWS profile, not inline credentials).

## Timeouts

`Timeouts` provides per-stage timeouts; any omitted field falls back to `DEFAULT_TIMEOUT_SECONDS` (900s):

- `ffmpeg`, `scenedetect`, `slides`, `transcribe`, `agno`, `s3`.
- `summarize` is optional; `None` falls back to `DEFAULT_TIMEOUT_SECONDS` at use time.

The legacy `elevenlabs` timeout key is silently dropped during load so old host configs still load; its value is ignored because the transcribe stage timeout is now `transcribe`.

## Loader behavior

`load(path)` parses by extension (`.json` → `json.loads`, `.yaml`/`.yml` → `yaml.safe_load`), validates the root is a mapping, then builds a `Config` through `_from_dict`. Field building is unified via `_build_section`, which applies dataclass defaults for omitted optional fields and raises `ConfigError` for missing required fields. Unsupported extensions and non-mapping roots raise `ConfigError`.

## Validation rules that raise ConfigError

- `stages.slides` must be a mapping `{enabled, backend}`; legacy bare bool rejected.
- `slides.backend` must be `"openrouter"`.
- `summary.backend` must be `"agno"`.
- `summary.language` must be `"en"` or `"original"`.
- `openrouter` section is required (unconditionally).
- `notion.token_env` required when `summary.backend == "agno"`.
- `debug` must be a boolean if present.
- Any missing required field or non-mapping section raises `ConfigError`.

Missing environment variables are **not** caught at config load. They surface at use time as `MissingEnvVarError` from `resolve_env`.

## Representative example configs

### acme — full setup (openrouter slides + agno summarize)

Annotated excerpts:

```yaml
recordings_dir: ./recordings

stages:
  slides:
    enabled: true
    backend: openrouter
  s3_sync: true

transcribe:
  model_id: microsoft/mai-transcribe-2
  diarize: true
  segment_seconds: 480
  overlap_seconds: 5

summary:
  language: original
  sections:
    - overview
    - key_points
    - action_items
  backend: agno

agent:
  output_file: "{basename}.md"

openrouter:
  api_key_env: OPENROUTER_API_KEY
  slides_model: google/gemini-2.0-flash-001
  summary_model: google/gemini-2.5-pro

notion:
  server: notion-acme
  parent_page_id: "REPLACE_WITH_PARENT_PAGE_ID"
  insert: subpage
  token_env: NOTION_API_KEY

telegram:
  bot_token_env: ACME_BOT_TOKEN
  default_chat_id: "REPLACE_WITH_DEFAULT_CHAT_ID"
  routing:
    topic-a: "REPLACE_WITH_TOPIC_A_CHAT_ID"
    topic-b: "REPLACE_WITH_TOPIC_B_CHAT_ID"

s3:
  bucket: "s3://acme-recordings/recordings/"
  profile: acme
```

This is the primary cost-saving combination: slides on `openrouter`, summarize on `agno` (OpenRouter model + direct Notion REST publish), with S3 sync and topic-routed Telegram.

### example — simpler setup (openrouter slides + agno summarize)

Annotated excerpts:

```yaml
recordings_dir: ./recordings

stages:
  slides:
    enabled: true
    backend: openrouter
  s3_sync: false

transcribe:
  model_id: microsoft/mai-transcribe-2
  diarize: true
  segment_seconds: 480
  overlap_seconds: 5

summary:
  language: en
  sections:
    - overview
    - key_points
    - action_items
  backend: agno

agent:
  output_file: "{basename}.md"

openrouter:
  api_key_env: OPENROUTER_API_KEY
  slides_model: google/gemini-2.0-flash-001
  summary_model: google/gemini-2.5-pro

notion:
  server: notion-example
  parent_page_id: "REPLACE_WITH_PARENT_PAGE_ID"
  insert: subpage
  token_env: NOTION_API_KEY

telegram:
  bot_token_env: TELEGRAM_BOT_TOKEN
  default_chat_id: "REPLACE_WITH_DEFAULT_CHAT_ID"
  routing: {}
```

This illustrates the other common backend combination: slides on `openrouter`, summarize on the `agno` backend, no S3 sync, single-chat Telegram, and `notion.token_env` present because summary runs on `agno`.

## Secrets-by-env-name rule

Every secret reference in config is an environment-variable **name string**, not a value:

- `openrouter.api_key_env`
- `telegram.bot_token_env`
- `notion.token_env`

The model stores these names; `resolve_env(var_name)` reads `os.environ` at use time and raises `MissingEnvVarError` if the variable is unset. This keeps secrets out of config files and out of the in-memory `Config` object.
