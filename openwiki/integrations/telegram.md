---
type: integration
title: Telegram Integration
description: How the transcriber sends meeting summaries to Telegram — bot token resolution, topic routing, Markdown stripping, message chunking, digest vs full-summary preference, and manifest-gated sends.
tags: [telegram, dissemination, bot-api, chunking, routing, manifests]
verified:
  - by: openwiki/0.6.1
    at: 2026-10-02T19:10:14.922Z
sources:
  - id: openwiki-source-dea2e4c42431c82993df8686
    resource: repo://tests/test_telegram.py
  - id: openwiki-source-df4110b2c5338913ae9eedcf
    resource: repo://transcriber/__main__.py
  - id: openwiki-source-bb626cf9b19692a8c8d8d744
    resource: repo://transcriber/agent.py
  - id: openwiki-source-5670223669d02f5c4fdf5d64
    resource: repo://transcriber/backends/summarize.py
  - id: openwiki-source-c4777b8db8d4806695ac8b6a
    resource: repo://transcriber/config.py
  - id: openwiki-source-00e3ed9e027733aaa6655e18
    resource: repo://transcriber/publish/telegram.py
  - id: openwiki-source-0c6dbb86c6b6001bf65f1b4c
    resource: repo://transcriber/state.py
generated: { by: "openwiki/0.6.1", at: "2026-10-02T19:10:14.922Z" }
---

# Telegram Integration

The transcriber disseminates summary text to Telegram chats through the Bot HTTP API (`sendMessage`). The integration lives in `transcriber/publish/telegram.py` and is orchestrated from `transcriber/__main__.py` as the fourth stage in the per-recording pipeline.

## Orchestration in the batch runner

Telegram is stage four of the per-recording flow (after `pipeline`, `describe_slides`, and `summarize`). The orchestrator in `transcriber/__main__.py` drives it as follows:

1. It checks the recording's manifest (`transcriber/state.py`) for the `"telegram"` key.
2. If the stage is already marked complete, the send is skipped (idempotent retry guard).
3. Otherwise, it reads the summary `.md` file and the digest `.telegram.md` file, picks between them (see below), and calls `TelegramPublisher(config).send(message_text)`.
4. On success, it calls `state.mark_complete("telegram")`.

<!-- openwiki: broken internal link [/transcriber/state.py] link "/transcriber/state.py" is root-absolute, which no real consumer resolves against the repository root (not a coding agent reading the page, not GitHub's Markdown renderer, not a local viewer); use a path relative to this file instead. Fix the href or restore the target, then delete this comment. -->
<!-- openwiki: broken internal link [/transcriber/__main__.py] link "/transcriber/__main__.py" is root-absolute, which no real consumer resolves against the repository root (not a coding agent reading the page, not GitHub's Markdown renderer, not a local viewer); use a path relative to this file instead. Fix the href or restore the target, then delete this comment. -->
Because `mark_complete` persists the manifest immediately after each stage, a crash anywhere in the pipeline leaves an accurate manifest for the next retry. This is what prevents duplicate Telegram sends when a batch is re-run after a partial failure ([transcriber/state.py](/transcriber/state.py), [transcriber/__main__.py](/transcriber/__main__.py)).

## What gets sent: digest vs full summary

The orchestrator decides which text to send by preferring the concise digest that the agent (`agy`) was told to write for chat, and falling back to the full summary only when the digest is missing or empty.

Concretely, in `transcriber/__main__.py` the logic is:

<!-- openwiki: broken internal link [/transcriber/agent.py] link "/transcriber/agent.py" is root-absolute, which no real consumer resolves against the repository root (not a coding agent reading the page, not GitHub's Markdown renderer, not a local viewer); use a path relative to this file instead. Fix the href or restore the target, then delete this comment. -->
- `digest_file = agent.digest_path_for(summary_file)` — derived from the summary path as `<name>.telegram.md` ([transcriber/agent.py](/transcriber/agent.py)).
- If the digest file exists and contains non-whitespace text, that text is used as `message_text`.
- Otherwise, a warning is logged and the full summary file is used instead.

<!-- openwiki: broken internal link [/transcriber/agent.py] link "/transcriber/agent.py" is root-absolute, which no real consumer resolves against the repository root (not a coding agent reading the page, not GitHub's Markdown renderer, not a local viewer); use a path relative to this file instead. Fix the href or restore the target, then delete this comment. -->
<!-- openwiki: broken internal link [/transcriber/backends/summarize.py] link "/transcriber/backends/summarize.py" is root-absolute, which no real consumer resolves against the repository root (not a coding agent reading the page, not GitHub's Markdown renderer, not a local viewer); use a path relative to this file instead. Fix the href or restore the target, then delete this comment. -->
This means the chat-side message is normally the shorter, chat-oriented digest; the full summary reaches Telegram only when the digest step did not produce output. The digest path derivation is shared by the orchestrator and the summarize backends ([transcriber/agent.py](/transcriber/agent.py), [transcriber/backends/summarize.py](/transcriber/backends/summarize.py)).

## Bot token resolution

The publisher never stores a literal secret. The Telegram config section (`transcriber/config.py`) holds only the **name** of the environment variable that contains the bot token (`telegram.bot_token_env`), along with `default_chat_id` and `routing`.

<!-- openwiki: broken internal link [/transcriber/publish/telegram.py] link "/transcriber/publish/telegram.py" is root-absolute, which no real consumer resolves against the repository root (not a coding agent reading the page, not GitHub's Markdown renderer, not a local viewer); use a path relative to this file instead. Fix the href or restore the target, then delete this comment. -->
<!-- openwiki: broken internal link [/transcriber/config.py] link "/transcriber/config.py" is root-absolute, which no real consumer resolves against the repository root (not a coding agent reading the page, not GitHub's Markdown renderer, not a local viewer); use a path relative to this file instead. Fix the href or restore the target, then delete this comment. -->
At send time, `TelegramPublisher._token()` resolves that variable via `transcriber.config.resolve_env`. If the variable is not set, the publisher raises `TelegramError` with an actionable message that names the missing variable (from `telegram.bot_token_env`). This keeps secret values out of the config model and out of logs under normal operation ([transcriber/publish/telegram.py](/transcriber/publish/telegram.py), [transcriber/config.py](/transcriber/config.py)).

<!-- openwiki: broken internal link [/transcriber/__main__.py] link "/transcriber/__main__.py" is root-absolute, which no real consumer resolves against the repository root (not a coding agent reading the page, not GitHub's Markdown renderer, not a local viewer); use a path relative to this file instead. Fix the href or restore the target, then delete this comment. -->
The pre-flight check (`transcriber check`) treats the Telegram token env var as always-required for dissemination: `_required_env_vars` includes `config.telegram.bot_token_env` unconditionally ([transcriber/__main__.py](/transcriber/__main__.py)).

## Topic routing

The publisher routes a message to a chat id based on its topic:

- If `topic` is non-empty and present in `config.telegram.routing`, the mapped chat id is used.
- If `topic` is `None`, empty, or not present in `routing`, the message falls back to `config.telegram.default_chat_id`.

<!-- openwiki: broken internal link [/transcriber/publish/telegram.py] link "/transcriber/publish/telegram.py" is root-absolute, which no real consumer resolves against the repository root (not a coding agent reading the page, not GitHub's Markdown renderer, not a local viewer); use a path relative to this file instead. Fix the href or restore the target, then delete this comment. -->
This is implemented in `resolve_chat_id(telegram, topic)` and used by `TelegramPublisher.send(text, topic)` ([transcriber/publish/telegram.py](/transcriber/publish/telegram.py)).

When the orchestrator sends the per-recording summary, it calls `send` without a topic, so each recording's message always lands in the default chat unless the publish path is extended to pass topic information.

## Markdown stripping

The digest is LLM-generated Markdown (bold, headings, bullets, links, inline code, blockquotes). Because `sendMessage` is called **without** a `parse_mode`, raw Markdown markers would otherwise render literally in the chat (the recipient would see `**`, `#`, `-`, and so on).

<!-- openwiki: broken internal link [/transcriber/publish/telegram.py] link "/transcriber/publish/telegram.py" is root-absolute, which no real consumer resolves against the repository root (not a coding agent reading the page, not GitHub's Markdown renderer, not a local viewer); use a path relative to this file instead. Fix the href or restore the target, then delete this comment. -->
Rather than enable a Telegram parse mode — MarkdownV2 requires pervasive escaping and rejects unescaped specials, and Telegram has no heading or list formatting anyway — the publisher strips Markdown to clean plain text before sending ([transcriber/publish/telegram.py](/transcriber/publish/telegram.py)).

`strip_markdown(text)` covers the Markdown the digest actually uses:

- `**bold**`, `__bold__`, `*italic*`, `_italic_` → inner text
- `` `code` `` → inner text (backticks removed)
- leading `#` heading markers → removed (Telegram has no headings)
- `-` / `*` / `+` list bullets → a `•` glyph (indentation preserved)
- `> quote` → quoted text (marker removed)
<!-- openwiki: broken internal link [url] file "url" does not exist. Fix the href or restore the target, then delete this comment. -->
- `[text](url)` → `text (url)` (link target preserved)
- `![alt](url)` → `alt (url)` or just the URL when there is no alt text
- fenced-code fences (```` ``` ````) → removed (content kept verbatim)

Line structure is preserved so the message stays readable. The sending path applies `strip_markdown` inside `send()` before chunking, so every transmitted chunk is clean plain text.

## Message chunking

Telegram enforces a hard limit of 4096 characters per text message (`TELEGRAM_MAX_MESSAGE_CHARS` in `transcriber/publish/telegram.py`).

If the (already Markdown-stripped) text exceeds that limit, `chunk_message(text, limit)` splits it into sequential chunks of at most `limit` characters. Splitting is purely by character count; the caller is responsible for any content-aware formatting. An empty string yields a single empty chunk so that at least one `sendMessage` call is made.

<!-- openwiki: broken internal link [/transcriber/publish/telegram.py] link "/transcriber/publish/telegram.py" is root-absolute, which no real consumer resolves against the repository root (not a coding agent reading the page, not GitHub's Markdown renderer, not a local viewer); use a path relative to this file instead. Fix the href or restore the target, then delete this comment. -->
The publisher then sends each chunk as a separate `sendMessage` POST to the same chat. The returned list of `httpx.Response` objects is one per chunk ([transcriber/publish/telegram.py](/transcriber/publish/telegram.py)).

## HTTP details

- Base URL: `https://api.telegram.org`
- Per-message endpoint: `https://api.telegram.org/bot<token>/sendMessage`
- Method: `POST`
- Payload: `{"chat_id": "...", "text": "..."}` — no `parse_mode` field is set.

<!-- openwiki: broken internal link [/transcriber/publish/telegram.py] link "/transcriber/publish/telegram.py" is root-absolute, which no real consumer resolves against the repository root (not a coding agent reading the page, not GitHub's Markdown renderer, not a local viewer); use a path relative to this file instead. Fix the href or restore the target, then delete this comment. -->
<!-- openwiki: broken internal link [/tests/test_telegram.py] link "/tests/test_telegram.py" is root-absolute, which no real consumer resolves against the repository root (not a coding agent reading the page, not GitHub's Markdown renderer, not a local viewer); use a path relative to this file instead. Fix the href or restore the target, then delete this comment. -->
The publisher accepts an optional pre-configured `httpx.Client` for test injection; when none is supplied, it creates a client per `send` call. Any `httpx.HTTPError` from a `sendMessage` call is surfaced as `TelegramError` with the failing chat id and the underlying error ([transcriber/publish/telegram.py](/transcriber/publish/telegram.py), [tests/test_telegram.py](/tests/test_telegram.py)).

## Manifest gating and retry safety

Telegram sends are manifest-gated. The orchestrator checks `state.is_complete("telegram")` before sending, and marks the stage complete only after a successful send. Combined with the per-stage manifest persistence in `RecordingState.mark_complete`, this means:

- A successful run records `"telegram": true` in `<name>.transcriber_state.json`.
- A later retry of the same recording skips the Telegram stage entirely.
- If a send fails, the stage is not marked complete, so a subsequent retry re-sends (once the upstream stages are also complete).

<!-- openwiki: broken internal link [/transcriber/state.py] link "/transcriber/state.py" is root-absolute, which no real consumer resolves against the repository root (not a coding agent reading the page, not GitHub's Markdown renderer, not a local viewer); use a path relative to this file instead. Fix the href or restore the target, then delete this comment. -->
<!-- openwiki: broken internal link [/transcriber/__main__.py] link "/transcriber/__main__.py" is root-absolute, which no real consumer resolves against the repository root (not a coding agent reading the page, not GitHub's Markdown renderer, not a local viewer); use a path relative to this file instead. Fix the href or restore the target, then delete this comment. -->
This is the same idempotency mechanism used for Notion pages and S3 syncs ([transcriber/state.py](/transcriber/state.py), [transcriber/__main__.py](/transcriber/__main__.py)).

## Configuration shape

The Telegram section of `config.yaml` (or the equivalent JSON) looks like:

```yaml
telegram:
  bot_token_env: "TELEGRAM_BOT_TOKEN"   # env-var NAME, not the token itself
  default_chat_id: "..."                # fallback chat for unmatched / no topic
  routing:                              # optional topic -> chat mapping
    engineering: "..."
    product: "..."
```

<!-- openwiki: broken internal link [/transcriber/config.py] link "/transcriber/config.py" is root-absolute, which no real consumer resolves against the repository root (not a coding agent reading the page, not GitHub's Markdown renderer, not a local viewer); use a path relative to this file instead. Fix the href or restore the target, then delete this comment. -->
All three fields are required at the dataclass level (`Telegram` in `transcriber/config.py`). The token value itself is resolved from the environment at use time, not stored in the config file ([transcriber/config.py](/transcriber/config.py)).

## Failure behavior

- Missing bot token env var → `TelegramError`, no HTTP request attempted.
- `sendMessage` HTTP error (non-2xx, network error) → `TelegramError`, stage not marked complete, recording marked as failed at the `"telegram"` stage in the batch outcome.
<!-- openwiki: broken internal link [/transcriber/__main__.py] link "/transcriber/__main__.py" is root-absolute, which no real consumer resolves against the repository root (not a coding agent reading the page, not GitHub's Markdown renderer, not a local viewer); use a path relative to this file instead. Fix the href or restore the target, then delete this comment. -->
- Failure isolation: an exception in one recording's Telegram stage is caught by the batch runner, logged with the stage name, recorded in that recording's outcome, and the batch continues to the next recording ([transcriber/__main__.py](/transcriber/__main__.py)).

## Related

- [Summarize & Publish workflow](../workflows/summarize-publish.md)
- [Pre-flight and idempotency](../operations/pre-flight-and-idempotency.md)
- `transcriber/publish/telegram.py` (implementation)
- `transcriber/__main__.py` (orchestration)
- `transcriber/config.py` (Telegram config model)
- `transcriber/state.py` (manifest / stage gating)
- `tests/test_telegram.py` (routing, stripping, chunking, error surfacing — httpx fully mocked)
