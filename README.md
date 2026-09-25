# transcriber

Local-first, AI-enabled meeting **transcriber engine**.

`transcriber` extracts the shared core of two ad-hoc, agent-driven meeting-
transcription setups into a single Python-native engine. It is designed to be
attached to a host repository as a **git submodule**: each host supplies only
its own config file, secrets (as environment variables), and a `recordings/`
directory. Per-host customizations live entirely in config, so the engine code
stays identical across hosts.

## Pipeline

The engine watches a `recordings_dir` for new `*.mp4` files and runs each
through the following stages:

1. **Audio extract** — pull an audio track from the video (`ffmpeg`).
2. **Scene extraction** *(optional, `stages.slides.enabled`)* — detect scene
   cuts and export slide frames (`scenedetect`). Network-free.
3. **Transcribe** — send audio to ElevenLabs (`transcribe.model_id`, e.g.
   `scribe_v1`) and receive a plain-text transcript.
4. **Describe slides** *(optional, only when `stages.slides.enabled`)* — turn the
   extracted slide images (+ transcript context) into a `<name>.slides.md`
   markdown block. Runs on the configured **slides backend**.
5. **Summarize** — turn the transcript (+ the `<name>.slides.md` block) into the
   final `<name>.md` summary and `<name>.telegram.md` digest, and publish a
   Notion subpage. Runs on the configured **summarize backend**. For the `agy`
   backend Notion is published via its MCP server; for the `agno` backend the
   **engine publishes the Notion subpage directly via the Notion REST API** (no
   MCP, no `npx`).
6. **Disseminate** — optionally sync the recording to S3 (`stages.s3_sync`), and
   post a notification to Telegram (topic-routed where configured).

Intermediate artifacts (including `<name>.slides.md`) are cleaned up when a
recording finishes processing.

### Post-transcript stages and per-stage backends

The two post-transcript stages each run on an independently selected **backend**:

| Stage | Config | Backends | Notes |
| --- | --- | --- | --- |
| `describe_slides` | `stages.slides.backend` | `agy` \| `openrouter` | `agy` = local CLI agent (today); `openrouter` = a direct OpenRouter vision call. |
| `summarize` | `summary.backend` | `agy` \| `agno` | `agy` = local CLI agent (today); `agno` = an Agno/OpenRouter model that produces the summary + digest, after which the **engine publishes the Notion subpage directly via the Notion REST API** (no MCP, no `npx`). |

The two selections are **orthogonal** — any combination is valid:

| slides | summarize | Meaning |
| --- | --- | --- |
| `agy` | `agy` | Today's behavior (the default when nothing is configured). |
| `openrouter` | `agy` | Primary cost saver — cheap vision, unchanged summary/publish. |
| `agy` | `agno` | Local slide description, OpenRouter-driven summarize. |
| `openrouter` | `agno` | Fully OpenRouter-backed post-transcript work. |

**Both stages default to `agy`**, so a config with no backend fields reproduces
today's behavior exactly (no OpenRouter/Agno egress, no new prerequisites).
There is **no silent cross-backend fallback**: a selected backend's failure fails
that stage (retryable) rather than switching to the other backend. Paid backend
calls live in manifest-gated orchestrator steps, so a retry never re-issues a
completed slides call; the `pipeline` stage itself stays network-free.

### Backend visibility (`--dry-run` / `check`)

Both `transcriber --dry-run` and `transcriber check` surface the selected backend
per stage so you can confirm the run's engine mix before it executes. A slides-on,
`openrouter` + `agno` plan reads:

```
audio-extract, scene-extract, transcribe, describe-slides [openrouter], summarize+notion [agno], telegram, s3-sync
```

## Configuration

A single config file drives the engine. The format is chosen by **file
extension**:

- `.json` → JSON
- `.yaml` / `.yml` → YAML

Both formats map to the identical validated model. Configs **never contain
secrets** — the Telegram bot token is referenced by environment-variable *name*
only and resolved at use time; S3 uses a named AWS profile.

See ready-to-adopt examples in [`examples/`](./examples):

- [`examples/example.config.yaml`](./examples/example.config.yaml) — slides on,
  English summaries, single Telegram chat, no S3.
- [`examples/acme.config.yaml`](./examples/acme.config.yaml) — S3 sync via a
  named profile, original-language summaries, topic-routed Telegram.

### Schema

| Field | Type | Notes |
| --- | --- | --- |
| `recordings_dir` | string | Directory watched for new `*.mp4` files. |
| `stages.slides` | object `{enabled, backend}` | Slide/scene extraction + description. `enabled: bool`; `backend: "agy" \| "openrouter"` (default `"agy"`). **Breaking change:** the legacy bare-bool form (`stages.slides: true`) is **no longer accepted** and raises a `ConfigError` — use the nested shape. |
| `stages.s3_sync` | bool | Enable S3 upload of recordings. |
| `transcribe.model_id` | string | ElevenLabs model id (e.g. `scribe_v1`). |
| `summary.language` | `"en"` \| `"original"` | Summary language. |
| `summary.sections` | list[string] | Summary sections to generate. |
| `summary.backend` | `"agy"` \| `"agno"` | Summarize backend. Default `"agy"`. `"agno"` runs an Agno/OpenRouter model for the summary + digest, then the engine publishes the Notion subpage via the REST API; requires the `openrouter` section and `notion.token_env`. |
| `agent.cli` | string | Headless agent CLI (e.g. `agy`). |
| `agent.extra_args` | list[string] | Extra CLI args. |
| `agent.output_file` | string | Output template, e.g. `{basename}.md`. |
| `openrouter` | object | *(optional)* Shared OpenRouter connection. Required iff `stages.slides.backend == "openrouter"` or `summary.backend == "agno"`. Fields: `api_key_env` (**env-var name** of the API key), `base_url` (default `https://openrouter.ai/api/v1`), `slides_model` (vision, default `google/gemini-2.0-flash-001`), `summary_model` (tool-capable, default `google/gemini-2.5-pro`), `max_slides` (deterministic slide-count ceiling for the `openrouter` slides backend; an over-ceiling deck hard-fails before any image is sent; default `60`), `slides_batch_size` (images per vision call; large decks are described in batches and the per-batch markdown concatenated, so a deck never overflows the model's context window; default `20`). |
| `notion.server` | string | Notion MCP server name (used by the `agy` backend). |
| `notion.parent_page_id` | string | Parent page for the new subpage. |
| `notion.insert` | string | Insertion mode (e.g. `subpage`). |
| `notion.token_env` | string | *(optional)* **Env-var name** of the Notion integration token. For the `agno` backend the engine uses it to publish the subpage via the Notion REST API. Required iff `summary.backend == "agno"`. |
| `telegram.bot_token_env` | string | **Env-var name** of the bot token. |
| `telegram.default_chat_id` | string | Fallback chat id. |
| `telegram.routing` | map[string,string] | Topic → chat id routes. |
| `timeouts` | map[string,int] | *(optional)* Per-stage timeouts (seconds); defaults to 900. Keys: `ffmpeg`, `scenedetect`, `slides` (the `describe_slides` stage), `elevenlabs`, `agy`, `summarize`, `s3`. `timeouts.summarize` is optional and **falls back to `timeouts.agy`** when unset (a networked `agno` run may need more time than a local `agy` run). |
| `s3` | object | *(optional)* `bucket` + `profile`. Required only when `stages.s3_sync` is true. |

Example (YAML):

```yaml
recordings_dir: ./recordings
stages:
  slides:
    enabled: true
    backend: agy        # "agy" | "openrouter"
  s3_sync: false
transcribe:
  model_id: scribe_v1
summary:
  language: en
  sections: [overview, key_points, action_items]
  backend: agy          # "agy" | "agno"
agent:
  cli: agy
  extra_args: [--dangerously-skip-permissions]
  output_file: "{basename}.md"
notion:
  server: notion-example
  parent_page_id: "REPLACE_WITH_PARENT_PAGE_ID"
  insert: subpage
telegram:
  bot_token_env: TELEGRAM_BOT_TOKEN
  default_chat_id: "REPLACE_WITH_DEFAULT_CHAT_ID"
  routing: {}
```

For a fully OpenRouter-backed mix (slides `openrouter` + summarize `agno`), add
an `openrouter` block and a `notion.token_env` — **env-var names only, no
secrets** — as shown in
[`examples/acme.config.yaml`](./examples/acme.config.yaml):

```yaml
stages:
  slides:
    enabled: true
    backend: openrouter
summary:
  backend: agno
openrouter:
  api_key_env: OPENROUTER_API_KEY   # env-var NAME only
  slides_model: google/gemini-2.0-flash-001
  summary_model: google/gemini-2.5-pro
notion:
  token_env: NOTION_API_KEY         # env-var NAME only; required for agno
```

### Privacy / data egress

Both non-`agy` backends send content to a **third party** (OpenRouter, and
Notion for `agno`):

- The slides `openrouter` backend uploads **slide imagery** plus transcript
  context; the `agno` summarize backend sends the transcript + slide markdown to
  the OpenRouter model, and the resulting summary to Notion via the REST API.
- Upstream **retention is outside the engine's control**.
- **Downscaling is a size/cost control, not redaction.** Reducing slide image
  resolution lowers—but does not eliminate—the fidelity of sensitive on-slide
  content; do not treat it as masking. Sensitive material still egresses.

Operators with sensitive content should keep **both** stages on `agy` (the
default), which performs no OpenRouter/Agno egress. Secrets (OpenRouter API key,
Notion token, auth headers) and image bytes are never logged.

## Prerequisites

- **Python** 3.10+ and [**uv**](https://docs.astral.sh/uv/) for dependency and
  environment management.
- [**ffmpeg**](https://ffmpeg.org/) on `PATH` — audio extraction.
- An **ElevenLabs** account and API key (env var) — transcription.
- The **`aws`** CLI configured with the profile referenced by `s3.profile`
  (only when `stages.s3_sync` is enabled).
- A headless coding agent (**`agy`**) authenticated and on `PATH` — required when
  either stage uses the `agy` backend (the default for both).

Additional prerequisites apply only when you select a non-`agy` backend:

- **OpenRouter API key** (env var named by `openrouter.api_key_env`) — required
  when `stages.slides.backend == "openrouter"` **or** `summary.backend == "agno"`.
- **The `agno` optional dependency** — required when `summary.backend == "agno"`.
  Install the extra with `uv sync --extra agno` (or `uv pip install
  'transcriber[agno]'`). It is *not* installed by default, so the all-`agy`
  configuration pulls in no OpenRouter/Agno dependencies.
- **Notion integration token** (env var named by `notion.token_env`) — required
  when `summary.backend == "agno"`. The engine uses this key to publish the
  Notion subpage via the **Notion REST API** (env-var name only in config; the
  value is resolved at use time and never logged). No Notion MCP server or
  Node.js/`npx` is needed for the `agno` backend.

## Usage

```bash
# Install deps into a managed environment.
uv sync

# Run against a host config (format chosen by extension).
uv run transcriber --config examples/acme.config.yaml

# Run tests.
uv run pytest -q
```

## Adopting as a git submodule

Host repos consume `transcriber` as a submodule and keep only their own config,
secrets, and recordings:

```bash
# 1. Add the engine as a submodule inside the host repo.
git submodule add <transcriber-repo-url> transcriber
git submodule update --init --recursive

# 2. Provide a host config (copy an example and edit ids/paths).
cp transcriber/examples/example.config.yaml ./my-host.config.yaml
#   edit recordings_dir, notion.parent_page_id, telegram ids, s3.* as needed.

# 3. Export the referenced secrets (names only live in config).
export TELEGRAM_BOT_TOKEN=...      # or ACME_BOT_TOKEN, per config
export ELEVENLABS_API_KEY=...

# 4. Run the engine from the host repo.
uv --directory transcriber run transcriber --config ../my-host.config.yaml
```

To update the engine later:

```bash
git submodule update --remote transcriber
```

> **Host maintainers — config migration.** This release replaces the legacy
> `stages.slides: <bool>` field with the nested `stages.slides: {enabled,
> backend}` shape (the bare-bool form now raises a `ConfigError`). Update your
> host config accordingly. If you choose `summary.backend: agno`, also install
> the `agno` extra (`uv sync --extra agno`) and set `notion.token_env` (plus the
> `openrouter` block); if you choose `stages.slides.backend: openrouter`, provide
> the `openrouter` block and its API key env var. Leaving both backends on `agy`
> (the default) keeps today's behavior with no new prerequisites.
