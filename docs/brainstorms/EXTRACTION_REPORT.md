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

## Resolved Decisions (from user answers)

| # | Decision | Impact |
|---|----------|--------|
| Q1 | **Git submodule** consumption model. | `transcriber` = engine; host repos add config + secrets + `recordings/`. |
| Q2 | **Orchestrator drives deterministic stages, then calls a headless agent** for stages 5–6. | Top-level is code, not a prompt. |
| Q3 | **Antigravity CLI (`agy`)** is the headless agent for iteration 1. | Must handle agy's non-TTY empty-output bug (#76) + interactive-approval stalls. |
| Q4 | **JSON *and* YAML** config (`transcriber.config.json` or `.yaml`/`.yml`). | Per-host stage toggles + settings + topic→chat routing; format chosen by file extension. |
| Q5 | **Upload (if S3 enabled) → then clear** intermediates. Keep `.mp4` + `.md`. | Cleanup gated on success, ordered after backup. |
| Q6 | **Drop PowerShell entirely.** Python + `uv` + `duct`. | Major pivot from the original seed ("pure pwsh"). Cross-platform-capable, single toolchain. |

> **Deviation flagged:** the seed spec asked for "pure pwsh scripts." The user has explicitly redirected to a Python-native engine (Python + `uv` + `duct`) with `agy` as the headless agent. All approaches below assume Python; the seed should be updated to reflect this when converted (`Seed` command).

### Research findings on the new stack

- **`duct` (duct.py):** child-process library with shell-like pipelines and IO redirection, errors-by-default. Fits chaining `ffmpeg`/`elevenlabs`/`scenedetect`/`aws`. API: `cmd("ffmpeg", ...).run()`, `.stdout_capture()`, `.stdout_to_file(path)`, `cmd(a).pipe(cmd(b))`.
- **`agy` headless is broken from non-TTY callers (upstream bug #76):** `agy -p "<prompt>"` prints nothing (empty string, exit 0) when stdout isn't a real terminal — i.e. exactly when called from a Python subprocess. On Windows a **ConPTY** (via `pywinpty`) is required.
- **`agy-headless-bridge` (PyPI) solves this:** `from agy_headless_bridge import run; run(prompt, add_dirs=["."], timeout=..., model=...)` allocates a fresh pty, runs `agy -p`, strips ANSI/TUI chrome, returns clean text. Verified on Windows with `agy 1.0.6`. Also exposes `AgyTimeoutError.partial` and an MCP server mode.
- **`agy` interactive-approval stall:** if `agy` pauses to ask "allow this tool call?" (e.g. Notion MCP write, shell), the headless run hangs until idle timeout. **Mitigation (user-chosen):** run agy with **`--dangerously-skip-permissions`** so it auto-proceeds without approval modals.
- **Output-via-file convention (user-chosen):** rather than parsing agy's stdout (subject to bug #76 and TUI noise), instruct agy in the prompt to **write its result to a named file**; Python reads the file afterward. This makes agy's console output irrelevant and the handoff deterministic.

## Architectural Approaches Evaluated

All approaches share: Python package managed by `uv`, `duct` for the deterministic media stages (ffmpeg → mp3, scenedetect → slides, elevenlabs → jsonl, parse → txt), a JSON/YAML config with stage toggles, git-submodule packaging, and success-gated cleanup that runs *after* optional S3 upload. They differ in **how the agent stage (5–6) is invoked** and **where the publishing/dissemination logic lives**.

### Approach A: Thin Python orchestrator + one big agent prompt (delegate everything)

- **Concept:** Python runs stages 1–4 with `duct`, then for each new recording builds one prompt (parsed transcript + slide descriptions + config-derived instructions for Notion/Telegram) and hands it to `agy` via `agy-headless-bridge`. The agent does summarize + Notion (its MCP) + Telegram (its skill) itself, mirroring today's `local-automation.md`. Python then does S3 upload + cleanup.
- **Component Changes:** `transcriber/` package: `pipeline.py` (duct stages), `agent.py` (bridge wrapper), `prompt_templates/` (summarize/slide/dissemination `.md`), `config.py` (JSON/YAML loader + schema). Host repo: `transcriber.config.json`/`.yaml`, `run.py` (thin entry), `recordings/`, agent MCP/skill config.
- **Dependencies Introduced:** `duct`, `agy-headless-bridge` (→ `pywinpty` on Windows), `agy` binary (auth), `uv`; slide stage keeps `scenedetect`/`av`. External CLIs unchanged (`ffmpeg`, `elevenlabs`, `aws`).

### Approach B: Python owns publishing/dissemination; agent only summarizes (deterministic side effects)

- **Concept:** Python runs 1–4, then calls `agy` **only to produce the Markdown summary** (pure text-in/text-out — no tool use, so no MCP-approval stalls and non-TTY reliability is the only agy concern). Python then does Notion (Notion REST API/SDK), S3 (`aws` via duct or boto3), and Telegram (HTTP Bot API) itself, driven entirely by the config's routing table. Cleanup last.
- **Component Changes:** Adds `publish/notion.py`, `publish/telegram.py`, `publish/s3.py` to the package; agent surface shrinks to a single summarize call. Config carries Notion tokens/parent IDs and the topic→chat routing table.
- **Dependencies Introduced:** Same as A **minus** reliance on agent-side MCP/skill, **plus** `httpx`/`requests` (Telegram + Notion REST) or `notion-client`. Removes the "agy must auto-approve tool calls" risk for publishing.

### Approach C: Config-driven stage plugins + agent-as-a-stage (most decoupled)

- **Concept:** A generic `Runner` executes an ordered list of `Stage` objects declared in config; each stage is a Python class (`AudioExtract`, `SlideExtract`, `Transcribe`, `Parse`, `Summarize`, `NotionPublish`, `S3Sync`, `TelegramDisseminate`). The `Summarize` stage internally uses the agent bridge. Host repos enable/disable/reorder stages purely via config (JSON or YAML). Publishing can be either agent-driven (A-style) or code-driven (B-style) per stage implementation.
- **Component Changes:** `stages/` package with a registry; `runner.py`; richer config schema (`{"stages": [{"type": "...", "enabled": true, "settings": {...}}]}` in JSON or the YAML equivalent). Highest structure, most upfront design.
- **Dependencies Introduced:** Superset of A/B depending on which stage impls ship.

## Structured Comparison & Methodology

### SWOT Matrix

| Approach | Strengths | Weaknesses | Opportunities | Threats/Risks |
|----------|-----------|------------|---------------|---------------|
| **A: delegate everything to agy** | Closest to current working setups; least new code; keeps Notion/Telegram logic in agent prompts (easy per-host tweak) | Fragile: agy must auto-approve MCP/tool calls or it stalls; non-deterministic publishing; hard to unit-test; empty-output bug surface is large | Fast first iteration; reuse existing `slide-extractor.md`/`local-automation.md` almost verbatim | agy #76 non-TTY bug; interactive-approval hangs; agent may hallucinate routing/IDs; token cost |
| **B: agy summarizes, Python publishes** | Deterministic, testable side effects; agy used only for text→text (safest agy mode); routing table is explicit config, not agent judgment; secrets handled in code | More code to write now (Notion/Telegram/S3 clients); must reimplement what agent MCP did | Reliable CI; reusable publish modules; language/topic routing enforced exactly | Notion/Telegram API changes; still depends on agy non-TTY reliability for the one summarize call |
| **C: stage-plugin framework** | Maximum flexibility & reuse; clean submodule story; host repos differ by config only | Over-engineered for 2 consumers; slowest to first working run; registry/schema design cost | Scales to N host repos & new stages later | YAGNI; premature abstraction may not match real 3rd consumer's needs |

**Methodology:** weighted against the seed's explicit constraints — headless reliability (highest weight, given agy #76), preserve-customizations, minimal-intermediate-cleanup correctness, and time-to-first-working-iteration. Reliability and testability separate B from A; simplicity separates B from C.

## Recommendation

**Adopt Approach B (agy summarizes, Python publishes) as the target, reached via a fast A-flavored first iteration.**

Rationale:
- **Reliability is the dominant constraint.** The agy #76 non-TTY bug and the interactive-approval stall make "delegate all tool use to agy headless" (Approach A) the riskiest exactly where it matters — automated, unattended runs. Restricting agy to **pure text→text summarization** (Approach B) uses the one mode that is verified working through `agy-headless-bridge`, and removes the MCP-auto-approve hazard entirely for publishing.
- **Customizations become explicit config, not agent judgment.** Topic→chat routing, Notion parent IDs, English-vs-original language, and stage on/off are precisely the divergences between scartill and adsight. Encoding them in `transcriber.config.json`/`.yaml` and executing them in tested Python (`publish/telegram.py`, `publish/notion.py`, `publish/s3.py`) preserves each host's behavior deterministically — the seed's "preserve customizations" requirement.
- **Cleanup correctness is trivial in code.** Q5's "upload if enabled, then clear" is a simple ordered, success-gated step in the orchestrator — hard to guarantee if an agent is driving.
- **Approach C is deferred, not rejected.** Structure B's publishers behind a thin stage interface so a future third consumer can graduate to the plugin model without a rewrite. Don't build the registry now (only two consumers).

**Pragmatic sequencing:** Iteration 1 may lean A-style (reuse existing prompts, let agy attempt Notion/Telegram via its MCP/skill) to get end-to-end fast — *but* gate it behind agy auto-approve config and treat it as throwaway. Land Approach B as the durable design.

### Key Risks & Mitigations

| Risk | Mitigation |
|------|------------|
| `agy -p` returns empty from Python (non-TTY bug #76) | **Don't consume agy's stdout at all** — instruct agy in the prompt to *write its output to a specified file* (e.g. `recordings/<name>.md`), then Python reads that file. This sidesteps the isatty()-gated stdout entirely. Still allocate a pty via `agy-headless-bridge` (`run()`) as belt-and-suspenders; assert the output file exists and is non-empty, fail loudly otherwise. |
| agy stalls on interactive tool-approval | Run agy with **`--dangerously-skip-permissions`** so it never blocks on approval modals. Combined with the file-output convention above, and `idle_timeout` as a backstop. (Approach B still keeps agy text-only where possible; the flag covers the iteration-1 A-style path and any tool use.) |
| Intermediate cleanup deletes data before S3 upload finishes | Strict ordering in orchestrator: summarize → publish → **S3 sync (if enabled) → verify → cleanup**. Cleanup only on success; `--keep-intermediates` debug flag. |
| Secrets (ElevenLabs, Notion, bot tokens, AWS) leaking into shared submodule | Secrets only in env vars referenced by name in JSON; `.gitignore` media/transcripts/config-with-secrets; never commit tokens. |
| Cross-platform drift (duct/pty differ POSIX vs Windows) | Target Windows first (verified stack); keep OS-specific bits (pty backend) inside the bridge; add smoke test per stage. |
| Python/uv toolchain now required where pwsh sufficed before | Ship `pyproject.toml` + `uv.lock`; document `uv run` entry; pin `agy-headless-bridge`, `duct`, `scenedetect`, `av`. |
| Divergent Notion insert semantics (subpage-under-parent vs prepend-link) | Model as a Notion strategy enum in config; implement both in `publish/notion.py`. |
| ElevenLabs/Notion/Telegram API or CLI changes | Wrap each in a small module with one integration point; pin `elevenlabs`/`aws` CLI expectations in README prerequisites. |

### Summary Table

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Engine language | Python (+ `uv`, `duct`) | User pivot (Q6); single toolchain, testable, cross-platform-capable. |
| Packaging | Git submodule | User (Q1); host repos add config + secrets + recordings only. |
| Orchestration | Python top-level; agent called per-recording | User (Q2). |
| Agent (iter 1) | `agy` via `agy-headless-bridge` | User (Q3) + non-TTY bug forces the bridge. |
| Agent role | **Summarize only** (text→text) | Reliability; avoids agy tool-approval stalls. |
| agy invocation | `--dangerously-skip-permissions` + **write output to a file** (Python reads the file, not stdout) | User-chosen: sidesteps approval stalls and the non-TTY stdout bug (#76). |
| Publishing/dissemination | **Python** (Notion API, Telegram Bot API, `aws`/duct) | Deterministic; encodes per-host customizations as config. |
| Config | `transcriber.config.json` or `.yaml`/`.yml` (stage toggles + settings + routing); format by extension | User (Q4) + YAML follow-up. |
| Cleanup | Success-gated, after optional S3 upload; keep `.mp4`+`.md` | User (Q5). |
| Architecture | Approach B, publishers behind thin stage interface | Reliability + testability now; path to Approach C later. |

## Phased Execution Plan

1. **Scaffold the Python engine** — `pyproject.toml` (uv), package `transcriber/`, deps: `duct`, `agy-headless-bridge`, `scenedetect`, `av`, `PyYAML` (YAML config); `transcriber.config.{json,yaml}` schema + a single loader that picks JSON or YAML by extension and validates into one internal model.
2. **Deterministic media stages (`pipeline.py`)** — port `Transcribe.ps1` logic to `duct`: mp4→mp3 (ffmpeg), optional scenedetect slides, elevenlabs jsonl, and port `parse_transcript.py` (keep as Python module, no more `uv run` shell-out). Idempotent `Test-Path`-equivalent guards. Emit a structured list of *new* recordings.
3. **Agent summarize stage (`agent.py`)** — invoke `agy` via `agy_headless_bridge.run()` with `--dangerously-skip-permissions` (passed through `extra_args`) so it never blocks on approval modals. Build prompt from parsed transcript + slide descriptions + `prompt_templates/`; force summary language per config; **instruct agy to write the summary to a target file** (e.g. `recordings/<name>.md`) rather than returning it on stdout. Python then reads that file. Assert the file exists and is non-empty; handle `AgyTimeoutError.partial` as a backstop.
4. **Publishers (`publish/`)** — `notion.py` (both insert strategies), `telegram.py` (Bot API, topic→chat routing, English-or-original per config), `s3.py` (aws sync via duct). All config-driven, unit-testable with mocked clients.
5. **Orchestrator + cleanup (`run.py`/`__main__`)** — order: discover → 1–4 → summarize → publish (Notion/Telegram) → S3 sync (if enabled) → verify → cleanup intermediates (keep `.mp4`+`.md`), `--keep-intermediates` flag.
6. **Submodule packaging & host wiring** — document/scaffold how `scartill-ai-hub` and `adsight/ai-hub` consume it: add submodule, drop in `transcriber.config.json` or `.yaml` reflecting each repo's current behavior, set env-var secrets, run `uv run transcriber`.
7. **Tests + README** — per-stage unit tests (mock ffmpeg/elevenlabs/agy/clients), a config-loader test asserting JSON and YAML parse to the same internal model, an end-to-end smoke test on a tiny fixture mp4, and a README covering prerequisites (ffmpeg, elevenlabs, aws, agy auth) and the config schema (both formats).
8. **Update the seed** — reconcile `docs/seed/extraction-brainstorm.md` with the Python pivot (via the `Seed` command) so downstream full-spec work matches reality.

