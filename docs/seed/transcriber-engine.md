# Seed: Generalize the two transcription setups into a reusable Python engine

## Intent

I run two ad-hoc, agent-driven meeting-transcription setups today:
- `../../scartill/scartill-ai-hub` (more advanced: slide extraction, Notion, single Telegram chat)
- `../../adsight/ai-hub` (S3 backup, topic-routed multi-channel Telegram)

Both are the same pipeline with different customizations. I want to extract the common core into **this `transcriber` app** so it can be attached to each host repo as a **git submodule**, while each host keeps only its own config + secrets + recordings.

Note: an earlier idea was "pure PowerShell scripts." I've since decided to go **Python-native** instead — drop PowerShell entirely.

## What it should do

A single Python entry point processes new `*.mp4` files in a `recordings/` directory through this pipeline:

1. **Audio extract** — `ffmpeg` mp4 → mp3.
2. **Slide/scene extraction** (optional) — `scenedetect` → slide images + scenes CSV.
3. **Transcribe** — `elevenlabs speech-to-text convert --model-id scribe_v2 --format jsonl` → `.jsonl`.
4. **Parse transcript** (optional) — jsonl → timestamped readable `.txt` (port the existing `parse_transcript.py` into a Python module; no more `uv run` shell-out).
5. **Summarize** — an AI agent produces an English Markdown summary (sections: Decisions, Action Items, Plans, Identified Risks), including slide descriptions where slides were extracted.
6. **Publish & disseminate** — Notion page, optional S3 backup, Telegram dissemination.

Every stage is idempotent (skip work whose output already exists) and only processes *new* recordings.

## Key decisions (from brainstorm)

- **Language/stack:** Python, managed by `uv`, using `duct` for child-process/pipeline work (ffmpeg, scenedetect, elevenlabs, aws). No PowerShell.
- **Packaging:** git submodule. `transcriber` ships the engine; each host repo adds its config, secrets (env vars), and `recordings/`.
- **Orchestration:** the Python engine is the top-level orchestrator. It runs the deterministic stages itself, then calls a headless agent per new recording.
- **Agent (iteration 1):** Antigravity CLI (`agy`).
  - Agent role is **summarize only** (text in → Markdown out). Publishing/dissemination is done in Python, not by the agent — this keeps behavior deterministic and testable and encodes each host's customizations as config rather than agent judgment.
  - Invoke `agy` with **`--dangerously-skip-permissions`** so it never blocks on approval prompts.
  - **Do not rely on `agy`'s console output.** Instruct `agy` to *write its result to a file*; Python reads that file. (This sidesteps the known `agy` non-TTY empty-stdout bug and TUI noise.)
- **Config:** per-host config file in **JSON or YAML** (`transcriber.config.json` / `.yaml` / `.yml`), format chosen by extension. It declares which stages run and their settings, including:
  - toggles: slide extraction, transcript parsing, S3 sync
  - Notion: target server/integration, parent page, insert strategy (create a subpage under the parent, then add its link at the **top** of the parent page)
  - Telegram: bot-token env var name, topic→chat routing table, language (English vs original)
  - S3: bucket + profile
  - summary language + sections
  - Secrets are referenced by env-var name, never stored in the config file.
- **Cleanup:** after a successful run, upload to S3 first (if enabled), then delete intermediate files. **Keep** the source `.mp4` and the final `.md` summary; delete `.mp3`, `.jsonl`, `.txt`, extracted slides, and scenes CSV. Provide a `--keep-intermediates` debug flag.

## Customizations to preserve

- **scartill:** slide extraction + transcript parsing on; Notion subpage under a fixed parent; single Telegram chat; English summaries.
- **adsight:** slide extraction on; Notion **subpage** policy (same as scartill); S3 sync on; topic-routed Telegram (per-topic chat IDs) in the original language; unidentified material to the CTO private chat.

In **both** hosts, the created subpage's link is added to the **top** of the base/parent page, not the bottom.

## Example config shape (illustrative, not final)

```yaml
recordings_dir: ./recordings
stages:
  slides: true
  parse_transcript: true
  s3_sync: false
transcribe:
  model_id: scribe_v2
summary:
  language: en           # or "original"
  sections: [Decisions, Action Items, Plans, Identified Risks]
agent:
  cli: agy
  extra_args: ["--dangerously-skip-permissions"]
  output_file: "{basename}.md"
notion:
  server: notion-private
  parent_page_id: "3dd6e913-f33d-8038-b4d6-f0df1cc4f68f"
  insert: subpage        # create subpage under parent, add its link at the TOP of the parent
telegram:
  bot_token_env: TELEGRAM_BOT_TOKEN
  default_chat_id: "247395877"
  routing:               # optional topic → chat map
    ECL2.0: "-4986569602"
    MagicWheel: "-5501481540"
s3:
  bucket: "s3://ai-hub.adsight/recordings/"
  profile: ai-hub
```

## Prerequisites (host environment)

Windows first. On `PATH`: `ffmpeg`, `elevenlabs` (authenticated), `aws` (profile as configured), `agy` (authenticated). Python + `uv`. Env vars for ElevenLabs auth, Notion integration token, Telegram bot token(s), AWS profile.

## Follow-up tasks

- Update `README.md` from "Local-first AI enabled transcriber" to describe the engine, config, and submodule usage.
- Document how `scartill-ai-hub` and `adsight/ai-hub` adopt the submodule (add submodule, drop in their config, set secrets, run).
- Provide example config files for both host repos reflecting their current behavior.
- Reconcile/replace the older `extraction-brainstorm.md` seed (which said "pure pwsh") with this one.
