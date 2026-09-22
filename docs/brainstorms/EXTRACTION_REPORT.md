# Technical Brainstorm: Generalizing Two Ad-hoc Transcription Setups into One Reusable App

## Problem Statement & Scope

- **Core Objective:** Extract the common core of two ad-hoc, agent-driven meeting-recording transcription pipelines (`scartill/scartill-ai-hub` and `adsight/ai-hub`) into this `transcriber` application, so it can be reused (e.g. attached as a git submodule) while each consuming project keeps its own customizations.
- **Scope Boundaries:**
  - **IN:** A shared, parameterized pipeline (video → audio → transcript → parsed text → summary → publish → disseminate); pure PowerShell orchestration that can call agents headless; a customization/config mechanism so each host repo overrides only its differences; cleanup of intermediate files after a successful run.
  - **OUT (initially):** Rewriting the actual summarization/LLM prompting logic beyond what's needed to invoke it; changing the external services (ElevenLabs, Notion, S3, Telegram); building a GUI; supporting non-Windows shells.
- **Key Constraints:**
  - Windows + PowerShell (`.ps1`, `$PSScriptRoot`).
  - External CLI dependencies: `ffmpeg`, `elevenlabs`, `uv`/`scenedetect`, `aws`, `telegram-cli`.
  - "Local-first" per README; media/transcripts are git-ignored and must not leak into the shared repo.
  - Must run **headless** — agent steps invoked non-interactively, no waiting for confirmation.
  - Intermediate files removed on completion.

## Technical Baseline & External Research

### Current Architecture (from local audit)

Both source repos follow the **same 6-stage skeleton**, differing only in which stages are present and how they're configured:

| Stage | scartill-ai-hub | adsight/ai-hub |
|-------|-----------------|----------------|
| 1. Audio extract (`ffmpeg` mp4→mp3) | ✅ | ✅ |
| 2. Slide/scene extraction (`uv run scenedetect`) | ✅ (`extracted_slides.*`, `*.scenes.csv`) | ❌ |
| 3. Transcribe (`elevenlabs ... scribe_v2 --format jsonl`) | ✅ | ✅ |
| 4. Parse JSONL → timestamped `.txt` (`parse_transcript.py` via `uv`) | ✅ | ❌ (summarizes from `.jsonl` directly) |
| 5. Summarize → English `.md` (agent) | ✅ English-forced, slide descriptions via `slide-extractor.md` | ✅ English-forced |
| 6a. Publish to Notion (MCP) | ✅ `notion-private`, subpage under fixed parent ID | ✅ `notion-adsight`, prepend link to page |
| 6b. Cloud backup (`aws s3 sync`) | ❌ | ✅ `Sync-ToCloud.ps1` → `s3://ai-hub.adsight/recordings/` |
| 6c. Telegram dissemination | ✅ single chat `247395877`, `scartill_bot`, English | ✅ topic-routed multi-chat, `adsight_bot`, **original language**, split by topic |

**Shared script core** (`Transcribe.ps1`): iterate `recordings/*.mp4`, idempotent guard on each output artifact (`Test-Path` before doing work), emit `New transcription:` marker lines that downstream agent steps key off.

**Orchestration today:** a `local-automation.md` prompt file per repo, read by an AI agent (Kiro/Orca) that runs the script then performs the agent-only stages (summarize, Notion, Telegram) inline. The prompt *is* the orchestrator; PowerShell only covers the deterministic media stages.

**Key divergences (the "customizations" to preserve):**
- Slide extraction present only in scartill.
- Transcript parsing step present only in scartill.
- S3 sync present only in adsight.
- Notion target (server name, parent page, insert strategy) differs.
- Telegram: single-chat English vs. topic-routed original-language with per-topic chat IDs and bot tokens.
- Bot tokens / env vars differ (`TELEGRAM_BOT_TOKEN` vs `ADSIGHT_BOT_TOKEN`).
- Summary sections are common (Decisions, Action Items, Plans, Identified Risks).

### State of the Art / Relevant Patterns

- **PowerShell module + manifest** (`.psm1`/`.psd1`) is the idiomatic way to ship reusable PS logic; host repos dot-source or `Import-Module`.
- **Config-over-code**: a per-host config file (JSON/PSD1) declaring which stages run and their parameters, consumed by a generic runner — the standard way to keep a shared engine while preserving per-consumer customization.
- **Git submodule** (as the user suggested) pins the shared engine at a commit; host repo supplies config + secrets + `recordings/`.
- **Headless agent invocation**: agent CLIs (Kiro CLI, aider, etc.) support a non-interactive/one-shot mode taking a prompt string/file, which is how PowerShell can "call agents headless."

### Transcriber project current state

Nearly empty: `docs/` (seed + this brainstorm), `.kiro` skill (`scartill-sdd-lite`), `README.md` ("Local-first AI enabled transcriber"). Greenfield — no legacy to preserve inside `transcriber` itself.

## Open Questions & User Clarifications

Please edit the answers inside the `USER_INPUT` tags. Defaults (assumptions) are stated after each.

#### Q1: Consumption model — submodule vs. package vs. copy
- **Context**: The seed says "can be then attached as e.g. a git submodule." That implies host repos (`scartill-ai-hub`, `adsight/ai-hub`) reference `transcriber` and add only config + secrets + recordings. Confirm this is the intended shape vs. a published PowerShell module or a scaffolding template that copies files in.
- **User Input**:
    <!-- USER_INPUT_START:Q1 -->
    Use dafault
    <!-- USER_INPUT_END:Q1 -->
- **Assumption if left default**: Git submodule. `transcriber` ships the engine (scripts/module + generic automation prompt); each host repo adds `transcriber.config.*`, a thin wrapper `Run.ps1`, secrets in env, and `recordings/`.

#### Q2: How much of the pipeline stays deterministic PowerShell vs. agent?
- **Context**: "Re-do into pure pwsh scripts, which can call agents headless." Stages 1–4 (ffmpeg, scenedetect, elevenlabs, parse) are already deterministic PS/CLI. Stages 5–6 (summarize, Notion, Telegram) are currently done *inline by the agent reading a prompt*. Should PowerShell become the top-level orchestrator that shells out to a headless agent for those stages, or should the agent remain the top-level driver that calls the PS scripts?
- **User Input**:
    <!-- USER_INPUT_START:Q2 -->
    Use default
    <!-- USER_INPUT_END:Q2 -->
- **Assumption if left default**: PowerShell is the top-level orchestrator. A single `Invoke-Transcriber.ps1` runs stages 1–4 deterministically, then for each new recording invokes a headless agent (one-shot prompt with the parsed transcript + config) for summarization/publishing/dissemination.

#### Q3: Which agent CLI is the headless target, and what is its non-interactive invocation?
- **Context**: Today Orca/Kiro reads `local-automation.md`. To call agents headless from pwsh we need the exact command (e.g. `kiro-cli chat --no-interactive --prompt-file ...`, `aider --message ...`, `orca ...`). MCP access (Notion) and skills (telegram-dissemination) must be available in that headless context.
- **User Input**:
    <!-- USER_INPUT_START:Q3 -->
    For the first iteration, use Antigravity CLI )`agy`)
    <!-- USER_INPUT_END:Q3 -->
- **Assumption if left default**: Kiro CLI in a headless/one-shot mode, with MCP servers (`notion-*`) and the `telegram-dissemination` skill configured in the host repo's agent config. Exact flag to be confirmed before implementation.

#### Q4: Customization surface — what must each host repo be able to override?
- **Context**: Need to decide the config schema. Candidate knobs: enable/disable slide extraction; enable/disable transcript parsing; enable/disable S3 sync (+ bucket/profile); Notion (server, parent page, insert mode); Telegram (bot token env var, chat routing table, language: English vs original); summary language; summary sections.
- **User Input**:
    <!-- USER_INPUT_START:Q4 -->
    Use JSON
    <!-- USER_INPUT_END:Q4 -->
- **Assumption if left default**: A single per-host `transcriber.config.psd1` (or `.json`) with a `stages` toggle list plus per-stage settings covering all knobs above. Topic→chat routing is a table in that config. Secrets stay in env vars referenced by name.

#### Q5: "Remove intermediate files when done" — what counts as intermediate vs. keep?
- **Context**: Artifacts per recording: `.mp3`, `.jsonl`, `.txt`, `extracted_slides.*/`, `*.scenes.csv`, final `.md`. Which are intermediate (delete after success) and which are durable outputs to keep/back up?
- **User Input**:
    <!-- USER_INPUT_START:Q5 -->
    Upload if enabled, then clear. Other as assumed.
    <!-- USER_INPUT_END:Q5 -->
- **Assumption if left default**: Keep the source `.mp4` and the final `.md` summary; delete `.mp3`, `.jsonl`, `.txt`, `extracted_slides.*`, `*.scenes.csv` **only after** the summary is produced and published successfully. Cleanup is gated on success and configurable (opt-out for debugging). Note: adsight's S3 sync currently backs up `.mp3`/`.jsonl` too — cleanup ordering must respect that (sync before delete, or exclude from cleanup).

#### Q6: Python dependency (`parse_transcript.py` via `uv`) — keep, port, or drop?
- **Context**: Only scartill parses JSONL→timestamped text via a Python script run with `uv`. This adds a Python/uv dependency to the shared engine. Options: keep as-is (bundle the .py + pyproject), port to native PowerShell (removes uv dependency), or drop parsing and summarize from raw `.jsonl`.
- **User Input**:
    <!-- USER_INPUT_START:Q6 -->
    More general change: avoid PowerShell completely, rely on Python, `uv`, and `duct`.
    <!-- USER_INPUT_END:Q6 -->
- **Assumption if left default**: Port `parse_transcript.py` to native PowerShell so the parsing stage has no Python dependency; keep `uv`/`scenedetect` only for the optional slide-extraction stage (Python is unavoidable there).

---

## Architectural Approaches Evaluated

_(Phase 3 — to be completed after user answers above.)_

## Structured Comparison & Methodology

_(Phase 3.)_

## Recommendation

_(Phase 4.)_

### Key Risks & Mitigations

_(Phase 4.)_

### Summary Table

_(Phase 4.)_
