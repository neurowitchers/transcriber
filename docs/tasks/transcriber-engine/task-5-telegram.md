# Task 5 — Telegram dissemination (`publish/telegram.py`)

**Status:** [ ]

**Spec:** `docs/specs/transcriber-engine.md`
**Dependencies:** Task 1 (config model). Independent of Tasks 2/3/4 — parallelizable.

## Target
`transcriber/publish/telegram.py`.

## Change
- Send summaries via the Telegram Bot HTTP API: `https://api.telegram.org/bot<token>/sendMessage`, using `httpx`.
- Token is read from the env var **named** in `config.telegram.bot_token_env`.
- Implement topic→chat routing: matched topics go to their chat id from `config.telegram.routing`; unmatched/unidentified content goes to `config.telegram.default_chat_id`.
- Message language per `config.summary.language` (English or original) — the caller supplies the text; this module routes and sends.
- Chunk messages that exceed Telegram's length limit into multiple `sendMessage` calls.

## Constraints
- No secrets in code or config literals — token via env var name only.
- Missing token env var → clear, actionable error.
- Pure HTTP; do not shell out to a `telegram-cli`.

## Ownership
Owns: `transcriber/publish/telegram.py` and its tests. Coordinate the `publish/` package `__init__` with Task 6 (which owns `publish/s3.py`) — additive only, no conflicting edits.

## Observable Acceptance
- **Tests (pytest, `httpx` client mocked):** routing resolves topics to chat ids incl. default fallback; long messages split correctly; correct `sendMessage` payloads asserted; missing token env → clear error.
- **Demo:** a mocked run shows correct `sendMessage` calls for a multi-topic summary.
