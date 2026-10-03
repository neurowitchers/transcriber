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
3. **Transcribe** — diarize and chunk the audio into explicit time slices and send each slice to **OpenRouter** speech-to-text (`transcribe.model_id`, an OpenRouter STT slug such as `microsoft/mai-transcribe-2`), then stitch the slices back into a single plain-text transcript. Chunk length and overlap are controlled by `transcribe.segment_seconds` / `transcribe.overlap_seconds`, and speaker labels by `transcribe.diarize`.
4. **Describe slides** *(optional, only when `stages.slides.enabled`)* — turn the
   extracted slide images into a `<name>.slides.md` markdown block via the
   **`openrouter` slides backend** (the only slides backend): one OpenRouter
   vision call **per slide**, image-only (no transcript). Each slide is
   described from its image alone; transcript cross-referencing happens in the
   summarize stage.
5. **Summarize** — turn the transcript (+ the `<name>.slides.md` block) into the
   final `<name>.md` summary and `<name>.telegram.md` digest, and publish a
   Notion subpage. Runs on the **`agno`** summarize backend: an Agno/OpenRouter
   model produces the summary + digest, after which the **engine publishes the
   Notion subpage directly via the Notion REST API** (no MCP, no `npx`).
6. **Disseminate** — optionally sync the recording to S3 (`stages.s3_sync`), and
   post a notification to Telegram (topic-routed where configured).

Intermediate artifacts (including `<name>.slides.md`) are cleaned up when a
recording finishes processing.

### Post-transcript stages and per-stage backends

The `describe_slides` stage always runs on the **`openrouter`** vision backend
(the only slides backend). The `summarize` stage runs on the **`agno`** backend:

| Stage | Config | Backends | Notes |
| --- | --- | --- | --- |
| `describe_slides` | `stages.slides.backend` | `openrouter` | The only slides backend: one OpenRouter vision call **per slide**, image-only (no transcript). Enabled by `stages.slides.enabled`. |
| `summarize` | `summary.backend` | `agno` | The only summarize backend: an Agno/OpenRouter model produces the summary + digest, after which the **engine publishes the Notion subpage directly via the Notion REST API** (no MCP, no `npx`). |

The slides descriptor is **image-only**: it describes what is visually on each
slide (title, key visual information, tables, Mermaid diagrams). Transcript
cross-referencing — aligning speaker commentary, decisions, and Q&A to slides —
is the summarize stage's job, which receives the full transcript **and** the
`<name>.slides.md` block.

`summarize` has a single valid choice:

| summarize | Meaning |
| --- | --- |
| `agno` | OpenRouter-driven summarize + direct Notion REST publish (the default and only backend). |

**`summarize` is always `agno`** (the default when nothing is configured). It
requires the `openrouter` block and its API key, plus `notion.token_env` for the
Notion REST publish. There is **no silent cross-backend fallback**: a selected
backend's failure fails that stage (retryable). Paid backend calls live in
manifest-gated orchestrator steps, so a retry never re-issues a completed slides
call; the `pipeline` stage itself stays network-free.

### Backend visibility (`--dry-run` / `check`)

Both `transcriber --dry-run` and `transcriber check` surface the selected backend
per stage so you can confirm the run's engine mix before it executes. A slides-on,
`agno`-summarize plan reads:

```
audio-extract, scene-extract, transcribe [openrouter:microsoft/mai-transcribe-2], describe-slides [openrouter], summarize+notion [agno], telegram, s3-sync
```

### Telegram topic routing

The **Disseminate** stage sends a concise digest to Telegram. How it is routed
depends on `telegram.routing`:

- **No routing (default, `routing: {}`)** — one digest is sent to
  `telegram.default_chat_id`.
- **Routing configured** — the final summary is **partitioned** into one digest
  **per routing topic** plus a *remainder*, in a **single** OpenRouter model
  call (the `agno` summarize model). Each topic's digest is sent to its chat id
  in `telegram.routing[topic]`; the remainder — everything the model did not
  assign to a listed topic — is sent to `telegram.default_chat_id`.

The partition is **constructive**: the model assigns each item to exactly one
bucket and places anything unmatched (or uncertain) into the remainder, so no
content is dropped and buckets do not overlap. A topic with no relevant content
produces an empty bucket and **no message is sent** to that chat — so a meeting
entirely about one topic sends only to that topic's chat and **nothing** to the
default. If the partition call fails or every bucket is empty, the engine falls
back to sending the single digest to `default_chat_id`, so a routing hiccup
never drops the notification.

`telegram.topic_descriptions` (optional) gives the model a short description of
each topic to classify more accurately; a topic without a description falls back
to its bare label. Each non-empty bucket is also written to a
`<name>.telegram.<topic>.md` intermediate file (topic slugified; cleaned up with
the other intermediates unless `debug`/`--keep-intermediates` is set).

```yaml
telegram:
  bot_token_env: TELEGRAM_BOT_TOKEN
  default_chat_id: "100"            # receives the remainder bucket
  routing:
    engineering: "201"
    product: "202"
  topic_descriptions:               # optional — steers classification only
    engineering: "backend/infra work, deploys, incidents"
    product: "roadmap, UX, product decisions"
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
| `stages.slides` | object `{enabled, backend}` | Slide/scene extraction + description. `enabled: bool`; `backend: "openrouter"` (the only slides backend; default `"openrouter"`). **Breaking change:** the legacy bare-bool form (`stages.slides: true`) is **no longer accepted** and raises a `ConfigError` — use the nested shape. The former `agy` slides backend has been removed. |
| `stages.s3_sync` | bool | Enable S3 upload of recordings. |
| `transcribe.model_id` | string | **OpenRouter speech-to-text model slug** (`vendor/model`, e.g. `microsoft/mai-transcribe-2`). Default `microsoft/mai-transcribe-2`. Audio is diarized, chunked into explicit time slices, sent to OpenRouter, and the slices are stitched back into the transcript. |
| `transcribe.diarize` | bool | Label speakers in the transcript. Default `true`. |
| `transcribe.segment_seconds` | int | Audio chunk length (seconds) sent per OpenRouter request. Default `480`. |
| `transcribe.overlap_seconds` | int | Overlap (seconds) between adjacent chunks, used when stitching the slices. Default `5`. |
| `summary.language` | `"en"` \| `"original"` | Summary language. |
| `summary.sections` | list[string] | Summary sections to generate. |
| `summary.backend` | `"agno"` | Summarize backend. Default (and only value) `"agno"`: runs an Agno/OpenRouter model for the summary + digest, then the engine publishes the Notion subpage via the REST API; requires the `openrouter` section and `notion.token_env`. The former `"agy"` backend has been removed. |
| `agent.output_file` | string | Output template, e.g. `{basename}.md`. (The legacy `agent.cli` / `agent.extra_args` fields drove the removed `agy` backend; they are now ignored if present.) |
| `openrouter` | object | **Required (always).** Transcription runs over the OpenRouter speech-to-text API, so this block is mandatory on every run (it is additionally used by the slides vision backend and the `agno` summarize backend). Fields: `api_key_env` (**env-var name** of the API key), `base_url` (default `https://openrouter.ai/api/v1`), `slides_model` (vision, default `google/gemini-2.0-flash-001`), `summary_model` (tool-capable, default `google/gemini-2.5-pro`), `max_slides` (deterministic slide-count ceiling for the slides backend; an over-ceiling deck hard-fails before any image is sent; default `60`). Each slide is sent in its own vision call (one image per call). |
| `notion.server` | string | Notion MCP server name. *(Legacy field retained for config compatibility; the `agno` backend publishes via the Notion REST API and does not use it.)* |
| `notion.parent_page_id` | string | Parent page for the new subpage. |
| `notion.insert` | string | Insertion mode (e.g. `subpage`). |
| `notion.token_env` | string | *(optional)* **Env-var name** of the Notion integration token. For the `agno` backend the engine uses it to publish the subpage via the Notion REST API. Required iff `summary.backend == "agno"`. |
| `telegram.bot_token_env` | string | **Env-var name** of the bot token. |
| `telegram.default_chat_id` | string | Fallback chat id. Also receives the **remainder** bucket when `telegram.routing` is set (see *Telegram topic routing* below). |
| `telegram.routing` | map[string,string] | Topic → chat id routes. When non-empty, the summary is partitioned into one digest **per topic** plus a remainder sent to `default_chat_id` (see *Telegram topic routing* below). Empty (the default) → a single digest to `default_chat_id`. |
| `telegram.topic_descriptions` | map[string,string] | *(optional)* Per-topic human-readable description used **only** to steer the routing-partition model call (which content belongs to which topic). Keys SHOULD match `telegram.routing` keys; a topic without a description falls back to its bare label. Has no effect when `telegram.routing` is empty. |
| `timeouts` | map[string,int] | *(optional)* Per-stage timeouts (seconds); defaults to 900. Keys: `ffmpeg`, `scenedetect`, `slides` (the `describe_slides` stage), `transcribe`, `summarize`, `s3`. The legacy `timeouts.elevenlabs` key is **renamed to `timeouts.transcribe`** and is silently ignored if left behind. `timeouts.summarize` is optional and **falls back to the default 900s** when unset. (The legacy `timeouts.agy` key has been removed and is ignored if left behind.) |
| `s3` | object | *(optional)* `bucket` + `profile`. Required only when `stages.s3_sync` is true. |
| `debug` | bool | *(optional, default `false`)* When `true`, intermediate artifacts are **kept** after a successful run (same effect as the `--keep-intermediates` CLI flag, but persistent in config). Preserves `.mp3`, `.slides.md`, `.telegram.md` (and any per-topic `.telegram.<topic>.md` routed digests), the `transcribe_work.<name>/` dir, extracted slides, and the scenes CSV for inspection while the tool matures. The CLI flag and `debug` are OR'd — either one keeps intermediates. |

Example (YAML):

```yaml
recordings_dir: ./recordings
stages:
  slides:
    enabled: true
    backend: openrouter   # the only slides backend
  s3_sync: false
transcribe:
  model_id: microsoft/mai-transcribe-2   # OpenRouter STT slug
  diarize: true
  segment_seconds: 480
  overlap_seconds: 5
summary:
  language: en
  sections: [overview, key_points, action_items]
  backend: agno         # the only summarize backend
agent:
  output_file: "{basename}.md"
openrouter:               # always required (transcription runs over OpenRouter)
  api_key_env: OPENROUTER_API_KEY   # env-var NAME only
  slides_model: google/gemini-2.0-flash-001
  summary_model: google/gemini-2.5-pro
notion:
  server: notion-example
  parent_page_id: "REPLACE_WITH_PARENT_PAGE_ID"
  insert: subpage
  token_env: NOTION_API_KEY         # env-var NAME only; required for agno
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

**Transcription now always egresses audio to a third party.** Every run uploads
the recording's audio (diarized and chunked into time slices) to **OpenRouter**
speech-to-text — there is no OpenRouter-free configuration. Enabling slides
and/or the `agno` summarize backend sends additional content to OpenRouter (and,
for `agno`, to Notion):

- **Transcription (every run):** audio slices are sent to the OpenRouter STT
  model named by `transcribe.model_id`. Per-run **`usage.cost`** is aggregated
  across slices and logged (dollars only; no audio content). Audio bytes are
  never logged.
- The slides `openrouter` backend uploads **slide imagery** (image-only, one
  image per call; no transcript); the `agno` summarize backend sends the
  transcript + slide markdown to the OpenRouter model, and the resulting summary
  to Notion via the REST API.
- Upstream **retention is outside the engine's control**.
- **Downscaling is a size/cost control, not redaction.** Reducing slide image
  resolution lowers—but does not eliminate—the fidelity of sensitive on-slide
  content; do not treat it as masking. (Downscaling does not apply to audio, which
  egresses in full.) Sensitive material still egresses.

Because transcription itself egresses to OpenRouter, there is **no fully
OpenRouter-free mode**. The `agno` summarize backend additionally sends the
transcript to OpenRouter and the summary to Notion. Operators with sensitive
content can still minimise egress by keeping slides **disabled**
(`stages.slides.enabled: false`), which avoids slide imagery — but the audio
transcription egress (and, for the summary, the `agno`/Notion egress) remains.
Secrets (OpenRouter API key, Notion token, auth headers), audio bytes, and image
bytes are never logged.

## Prerequisites

- **Python** 3.10+ and [**uv**](https://docs.astral.sh/uv/) for dependency and
  environment management.
- [**ffmpeg**](https://ffmpeg.org/) on `PATH` — audio extraction.
- An **OpenRouter API key** (env var named by `openrouter.api_key_env`) —
  **always required**: transcription runs over the OpenRouter speech-to-text
  API, so the `openrouter` block and its key are mandatory on every run.
- The **`aws`** CLI configured with the profile referenced by `s3.profile`
  (only when `stages.s3_sync` is enabled).
- **The `agno` optional dependency** — the `summarize` stage always runs on the
  `agno` backend. Install the extra with `uv sync --extra agno` (or `uv pip
  install 'transcriber[agno]'`).
- **Notion integration token** (env var named by `notion.token_env`) — required
  by the `agno` summarize backend. The engine uses this key to publish the
  Notion subpage via the **Notion REST API** (env-var name only in config; the
  value is resolved at use time and never logged). No Notion MCP server or
  Node.js/`npx` is needed.

Additional prerequisites apply only when you enable slides:

- The slides `openrouter` vision backend uses the same `openrouter` block and
  API key already required for transcription; no extra dependency is needed.

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
export OPENROUTER_API_KEY=...      # always required (transcription runs over OpenRouter)

# 4. Run the engine from the host repo.
uv --directory transcriber run transcriber --config ../my-host.config.yaml
```

To update the engine later:

```bash
git submodule update --remote transcriber
```

> **Host maintainers — config migration.** This release moves transcription
> from the ElevenLabs CLI to the **OpenRouter speech-to-text API**:
> `transcribe.model_id` is now an **OpenRouter STT slug** (`vendor/model`,
> default `microsoft/mai-transcribe-2`) — replace any `scribe_v1`/ElevenLabs id,
> and drop the ElevenLabs CLI/account. The `openrouter` block is now **always
> required** (it is the transcriber), so provide it and its API-key env var on
> every run; the `transcribe` section gains `diarize` (default `true`),
> `segment_seconds` (default `480`), and `overlap_seconds` (default `5`). The
> `timeouts.elevenlabs` key is **renamed to `timeouts.transcribe`**; the legacy
> key is silently ignored if left behind. This release also replaces the legacy
> `stages.slides: <bool>` field with the nested `stages.slides: {enabled,
> backend}` shape (the bare-bool form now raises a `ConfigError`), and **removes
> the `agy` slides backend** — `stages.slides.backend` now accepts only
> `openrouter` (the default). Update your host config accordingly: if you
> previously ran slides on `agy`, either switch to `openrouter` (provide the
> `openrouter` block and its API-key env var) or disable slides
> (`stages.slides.enabled: false`). The former `openrouter.slides_batch_size`
> field is gone (each slide is now sent in its own vision call) and is silently
> ignored if left in a config. This release also **removes the `agy` summarize
> backend**: `summary.backend` now accepts only `agno` (the default). If you
> previously ran `summary.backend: agy`, switch to `agno` — install the `agno`
> extra (`uv sync --extra agno`) and set `notion.token_env` (plus the
> `openrouter` block). The legacy `agent.cli` / `agent.extra_args` and
> `timeouts.agy` fields are no longer used and are silently ignored if left in a
> config. Keeping slides disabled avoids the slides egress, but the `openrouter`
> block and its API key remain required because transcription itself runs over
> OpenRouter, and `agno` additionally egresses the transcript to OpenRouter and
> the summary to Notion.
