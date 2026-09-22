# Specification Critique: Transcriber Engine

**Target Specification:** `docs/specs/transcriber-engine.md`  
**Derived From:** `docs/seed/transcriber-engine.md` and `docs/brainstorms/EXTRACTION_REPORT.md`  
**Date:** 2026-09-22 (Updated following stakeholder feedback)  

---

## Executive Summary

The specification for `docs/specs/transcriber-engine.md` presents a well-structured, modular plan for extracting shared meeting transcription pipelines into a reusable Python engine. The transition from legacy PowerShell scripts to a `uv`-managed Python package with `duct` orchestration and git submodule packaging is technically sound and directly addresses host divergence between `scartill-ai-hub` and `adsight/ai-hub`.

Following stakeholder review, **X1 (Notion responsibility split via `agy` MCP)** was explicitly disregarded as an accepted design choice (Notion publishing will remain agent-driven via MCP). 

The primary remaining **Must-Address finding** focuses on failure recovery and state tracking:

1. **State & Recovery Tracking Gap (E1):** Idempotency based solely on intermediate artifact file existence (`.mp3`, `.jsonl`, `.md`) fails to track multi-step publishing status (Notion vs Telegram vs S3). Partial pipeline failures will either create duplicate Notion/Telegram entries or skip publishing entirely on retries.

**Verdict:** ⚠️ **PROCEED WITH UPDATES** — Resolving the idempotency state model (E1) and adding operational guardrails (pre-flight checks, non-XML prompt encapsulation) in `docs/specs/transcriber-engine.md` is recommended before starting implementation.

---

## Product Lens Findings

### 1a. Problem Validation
- **Submodule UX & Developer Workflow:** Packaging `transcriber` as a git submodule is appropriate for sharing code across `scartill-ai-hub` and `adsight/ai-hub`. However, developer workflows (working with detached HEAD, submodule initialization, and standalone testing within the engine repo) need clear specification so developer friction is minimized.

### 1b. User Value Assessment
- **Agent-Driven Notion Publishing (X1 - Disregarded / Accepted):** Stakeholder explicitly confirmed that Notion subpage creation and top-of-page link insertion via `agy` MCP tools is the intended design. While non-deterministic LLM layout behavior remains a risk, this keeps Notion logic inside agent prompt workflows as desired by the team.

### 1c. Alternative Approaches
- **Direct CLI & Binary Integrations:** Deterministic media stages use `duct` to call `ffmpeg`, `scenedetect`, and `elevenlabs`, keeping heavy processing decoupled from LLM execution.

### 1d. Edge Cases & User Experience
- **Lack of Preview / Dry-Run Mode:** Users running `transcriber` on a production recording directory cannot inspect which `.mp4` files will be processed, what stages will trigger, or where Notion/Telegram updates will go without risking unintended side effects or file deletions.

### 1e. Success Measurement
- **Batch Run Summaries:** The spec lacks a defined format for execution summaries or reporting when a batch completes with mixed success (e.g., 2 recordings succeeded, 1 failed during audio extraction).

---

## Engineering Lens Findings

### 2a. Architecture Soundness
- **Summarization + Notion via `agy` (Accepted):** Task 4 combines LLM Markdown generation with Notion subpage creation in a single `agy` prompt. `agent.py` must handle `agy` timeouts and partial outputs gracefully.

### 2b. Failure Mode Analysis
- **Retry Idempotency and Duplicate Side-Effects (E1):** Artifact existence checks (`Test-Path` logic ported from PS1) do not capture publishing completion. If Telegram dissemination fails due to network outage after Notion page creation, re-running `transcriber` will either re-prompt `agy` (creating duplicate Notion pages) or skip processing if `.md` exists (leaving Telegram unposted).
- **Unbound Child Process Executions (E3):** Commands executed via `duct` (`ffmpeg`, `scenedetect`, `elevenlabs`, `aws s3 sync`) lack explicit timeouts. Malformed media files or hung network sockets could block orchestrator execution indefinitely.

### 2c. Security & Privacy Review
- **Prompt Injection Defense (X2 - Updated: Non-XML Encapsulation):** Passing un-sanitized transcript text into `agy` with `--dangerously-skip-permissions` enables potential prompt injection attacks embedded within meeting audio/speech. Input transcripts must be wrapped using strict non-XML structural encapsulation (e.g., Markdown triple-backtick code fences / structural block quotes) within prompt templates.

### 2d. Performance & Scalability
- **Sequential Pipeline Isolation:** Sequential execution per recording is appropriate for meeting processing workloads. Storage cleanup on per-recording success effectively bounds disk usage during large batch runs.

### 2e. Testing Strategy
- **Integration Testing for Task 4:** Mocking `agy` in Task 4 requires simulating LLM output alongside MCP tool calls. Unit tests should assert bridge invocation arguments and fixture readbacks.

### 2f. Operational Readiness
- **Pre-Flight Environment Validation (E2):** The orchestrator assumes external CLI binaries (`ffmpeg`, `scenedetect`, `elevenlabs`, `aws`, `agy`) and environment variables (`TELEGRAM_BOT_TOKEN`, Notion tokens) exist. Lack of pre-flight checks causes opaque mid-pipeline failures.

### 2g. Dependencies & Integration Risks
- **`agy-headless-bridge` Platform Support:** As noted in spec risks, PTY support in `agy-headless-bridge` on POSIX is unverified. Ensuring clear fallback or explicit Windows-first scoping is necessary.

---

## Cross-Lens Insights

Items where Product value and Engineering soundness converge:

1. **Explicit Manifest / Status Tracking (E1):**
   - *Product Lens:* Prevents duplicate Telegram posts and duplicate Notion subpages when re-running after temporary network errors.
   - *Engineering Lens:* Provides precise, resumeable stage-level idempotency state instead of relying on file existence heuristics.

2. **Pre-Flight Validation & Dry-Run Mode (E2 × P1):**
   - *Product Lens:* Allows operators to inspect pipeline targets and verify host configurations safely.
   - *Engineering Lens:* Fails fast on missing binaries or environment variables before executing expensive media extractions.

3. **Non-XML Transcript Encapsulation (X2):**
   - *Product Lens:* Protects summary formatting from prompt manipulation.
   - *Engineering Lens:* Failsafe input boundary definition without using XML tags.

---

## Findings Summary Table

| ID | Lens | Severity | Category | Finding | Suggestion |
|---|---|---|---|---|---|
| X1 | Both | ⚪ *Disregarded* | Responsibility Split | Task 4 delegates Notion page creation and top-of-page link insertion to `agy` prompt via MCP. | *Accepted design choice per stakeholder feedback.* Keep Notion publishing in `agy` prompt as specified. |
| E1 | Engineering | 🎯 | Failure Modes & State | File existence checks (`.md`, `.jsonl`) cannot track publishing state (Notion, Telegram, S3). Retries on failure cause duplicate pages/messages or skipped publishes. | Add explicit per-recording status tracking (e.g. `.transcriber_state.json` or status manifest) to isolate stage completion cleanly. |
| E2 | Engineering | 💡 | Operational Readiness | Missing pre-flight CLI binary and environment variable checks before starting long-running pipeline execution. | Add pre-flight validation in orchestrator startup and a `transcriber check` subcommand to verify `ffmpeg`, `elevenlabs`, `agy`, etc. |
| E3 | Engineering | 💡 | Failure Mode Analysis | `duct` process calls for `ffmpeg`, `scenedetect`, `elevenlabs`, and `aws` lack timeouts, risking hung execution on bad inputs. | Add configurable timeouts and structured execution wrappers in `pipeline.py` and `publish/s3.py`. |
| P1 | Product | 💡 | Edge Cases & UX | No CLI `--dry-run` flag to preview pending recordings, planned stage executions, and destination targets without side effects. | Add `--dry-run` flag to `__main__.py` that logs execution plan without invoking subprocesses or deleting files. |
| X2 | Both | 💡 | Security & Privacy | Unsanitized transcript content passed to `agy` with `--dangerously-skip-permissions` creates a prompt injection surface. | Enforce strict non-XML structural encapsulation (Markdown code blocks/fences) in `prompt_templates/` for transcript data. |

---

## Verdict

### ⚠️ PROCEED WITH UPDATES

The specification `docs/specs/transcriber-engine.md` is mature and ready to proceed once **E1** (per-recording state tracking) and operational recommendations (E2, E3, P1, X2) are incorporated into the task breakdown.

---

## Proposed Spec Remediation

Here are the precise proposed edits for `docs/specs/transcriber-engine.md`:

### Remediation for E1 (State & Recovery Tracking)

**Update Requirement R8 in `docs/specs/transcriber-engine.md`:**
```markdown
R8. Responsibility split:
   - **Agent (agy):** summarize (write `.md`) and publish to Notion via its `notion-*` MCP.
   - **Python:** deterministic media stages, Telegram dissemination (Bot HTTP API), S3 sync, cleanup, and per-recording stage status tracking (`.transcriber_state.json`).
```

### Remediation for X2 (Non-XML Transcript Encapsulation)

**Update Task 4 in `docs/specs/transcriber-engine.md`:**
```markdown
### Task 4 — Agent stage (`agent.py`): summarize + Notion via agy
- **Objective:** Build prompt from `.txt`/`.jsonl` + slide images + `prompt_templates/` (using Markdown block fences for transcript content encapsulation; no XML tags). Invoke `agy` via `agy_headless_bridge.run(prompt, add_dirs=[recording_dir], extra_args=["--dangerously-skip-permissions"], ...)`, instructing it to (a) write summary `.md` and (b) create Notion subpage & link at top of parent via MCP. Handle `AgyTimeoutError.partial`.
- **Tests:** Prompt-builder units (verify Markdown code-fence encapsulation); agy mocked call assertions.
```

### Remediation for E2, E3 & P1 (Pre-flight check, Timeouts, & Dry-run mode)

**Update Task 7 in `docs/specs/transcriber-engine.md`:**
```markdown
### Task 7 — Orchestrator, CLI entry, & Pre-flight check (`__main__.py`)
- **Objective:** `uv run transcriber [--config PATH] [--keep-intermediates] [--dry-run]`:
  1. Pre-flight check: verify binaries (`ffmpeg`, `elevenlabs`, `aws`, `agy`) on `PATH` and env vars set.
  2. Process child commands (`duct`) with configurable execution timeouts.
  3. If `--dry-run`: discover new `*.mp4`, print execution plan, exit.
  4. Batch execution: process stages with per-recording `.transcriber_state.json` status tracking for resumeability on partial failures.
- **Tests:** Pre-flight failure on missing binary; timeout enforcement; dry-run output assertions; failure isolation across recordings.
```
