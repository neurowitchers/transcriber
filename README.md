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
2. **Slide / scene extraction** *(optional, `stages.slides`)* — detect scene
   cuts and export slide frames (`scenedetect`).
3. **Transcribe** — send audio to ElevenLabs (`transcribe.model_id`, e.g.
   `scribe_v1`) and receive a timestamped transcript.
4. **Transcript parse** *(optional, `stages.parse_transcript`)* — normalize the
   raw transcript into clean, readable text.
5. **Summarize** — drive a headless coding agent (`agent.cli`, e.g. `agy`) to
   produce a structured summary in the configured language and sections.
6. **Publish & disseminate** — create a Notion subpage under the configured
   parent page, optionally sync the recording to S3 (`stages.s3_sync`), and post
   a notification to Telegram (topic-routed where configured).

Intermediate artifacts are cleaned up when a recording finishes processing.

## Configuration

A single config file drives the engine. The format is chosen by **file
extension**:

- `.json` → JSON
- `.yaml` / `.yml` → YAML

Both formats map to the identical validated model. Configs **never contain
secrets** — the Telegram bot token is referenced by environment-variable *name*
only and resolved at use time; S3 uses a named AWS profile.

See ready-to-adopt examples in [`examples/`](./examples):

- [`examples/example.config.yaml`](./examples/example.config.yaml) — slides +
  transcript parse, English summaries, single Telegram chat, no S3.
- [`examples/acme.config.yaml`](./examples/acme.config.yaml) — S3 sync via a
  named profile, original-language summaries, topic-routed Telegram.

### Schema

| Field | Type | Notes |
| --- | --- | --- |
| `recordings_dir` | string | Directory watched for new `*.mp4` files. |
| `stages.slides` | bool | Enable slide/scene extraction. |
| `stages.parse_transcript` | bool | Enable transcript normalization. |
| `stages.s3_sync` | bool | Enable S3 upload of recordings. |
| `transcribe.model_id` | string | ElevenLabs model id (e.g. `scribe_v1`). |
| `summary.language` | `"en"` \| `"original"` | Summary language. |
| `summary.sections` | list[string] | Summary sections to generate. |
| `agent.cli` | string | Headless agent CLI (e.g. `agy`). |
| `agent.extra_args` | list[string] | Extra CLI args. |
| `agent.output_file` | string | Output template, e.g. `{basename}.md`. |
| `notion.server` | string | Notion MCP server name. |
| `notion.parent_page_id` | string | Parent page for the new subpage. |
| `notion.insert` | string | Insertion mode (e.g. `subpage`). |
| `telegram.bot_token_env` | string | **Env-var name** of the bot token. |
| `telegram.default_chat_id` | string | Fallback chat id. |
| `telegram.routing` | map[string,string] | Topic → chat id routes. |
| `timeouts` | map[string,int] | *(optional)* Per-stage timeouts (seconds); defaults to 900. Keys: `ffmpeg`, `scenedetect`, `elevenlabs`, `agy`, `s3`. |
| `s3` | object | *(optional)* `bucket` + `profile`. Required only when `stages.s3_sync` is true. |

Example (YAML):

```yaml
recordings_dir: ./recordings
stages:
  slides: true
  parse_transcript: true
  s3_sync: false
transcribe:
  model_id: scribe_v1
summary:
  language: en
  sections: [overview, key_points, action_items]
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

## Prerequisites

- **Python** 3.10+ and [**uv**](https://docs.astral.sh/uv/) for dependency and
  environment management.
- [**ffmpeg**](https://ffmpeg.org/) on `PATH` — audio extraction.
- An **ElevenLabs** account and API key (env var) — transcription.
- The **`aws`** CLI configured with the profile referenced by `s3.profile`
  (only when `stages.s3_sync` is enabled).
- A headless coding agent (**`agy`**) authenticated and on `PATH` —
  summarization.

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
