# Task 4 — Agent stage (`agent.py`): summarize + Notion via agy

**Status:** [ ]

**Spec:** `docs/specs/transcriber-engine.md`
**Dependencies:** Task 1 (config model). Can build prompt templates in parallel; integrates at wiring.

## Target
`transcriber/agent.py` and `transcriber/prompt_templates/`.

## Change
- Port `slide-extractor.md` and the summary-sections instructions into `prompt_templates/`.
- Build the agent prompt from the parsed `.txt` (or `.jsonl`) + slide images + templates.
  - Force the summary language per `config.summary.language`.
  - Include the section list from `config.summary.sections` (Decisions, Action Items, Plans, Identified Risks).
  - Include slide-description sections only when slides were extracted.
  - **Wrap transcript content using non-XML structural encapsulation (Markdown code fences / block quotes) — never XML tags** — to bound the prompt-injection surface (X2).
- Invoke `agy` via `agy_headless_bridge.run(prompt, add_dirs=[recording_dir], extra_args=["--dangerously-skip-permissions"], timeout=config.timeouts.agy, ...)`, instructing agy to:
  (a) write the summary to the configured `output_file` (templated with `{basename}`), and
  (b) create a Notion subpage under `config.notion.parent_page_id` via its `notion-*` MCP and add the subpage link at the **top** of the parent page.
- Read the summary file back; assert it exists and is non-empty; handle `AgyTimeoutError.partial`.

## Constraints
- Do **not** consume agy's stdout as the source of truth — the summary comes from the file agy writes.
- Notion publishing is intentionally agent-driven (accepted design); do not add a Python Notion client here.
- No XML tags anywhere in transcript encapsulation.

## Ownership
Owns: `transcriber/agent.py`, `transcriber/prompt_templates/`, and their tests. Does not own Telegram/S3/cleanup or the orchestrator.

## Observable Acceptance
- **Tests (pytest):** prompt-builder units — language forced per config; slide sections only when slides exist; sections from config; **transcript wrapped in Markdown code fences, no XML**. Agy call mocked to write a fixture file → assert readback + non-empty validation + `AgyTimeoutError.partial` handling.
- **Demo:** with mocked (or real, authenticated) agy, a `.txt` produces `<name>.md`.
