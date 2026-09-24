# Task 5 — Orchestrator wiring (`__main__.py` + `state.py`)

**Status:** [ ]

**Spec:** `docs/specs/slide-description-openrouter.md`
**Dependencies:** Task 1 (config), Task 3 (slides backends + helper), Task 4
(summarize backends). Integration hub — schedule after 3 and 4.

## Target
`transcriber/state.py`, `transcriber/__main__.py`, and `tests/test_main.py`
(+ `tests/test_state.py` if present).

## Change
- `state.py`: set the tuple to **exactly**
  `STAGES = (pipeline, describe_slides, summarize, notion, telegram, s3, cleanup)`
  (`describe_slides` inserted after `pipeline`). Update the docstring "Tracked
  stages" list. Note in the docstring that pre-existing manifests lack
  `describe_slides` and are treated as incomplete (`_completed.get(stage, False)`),
  so a re-run re-issues the slides call exactly once.
- `__main__._process_one`: add a manifest-gated `describe_slides` step (only when
  `stages.slides.enabled`): build slide inputs (Task 3), read the transcript, run
  the configured **slides backend** (Task 3), write `<name>.slides.md`
  (idempotent skip if present), log stage/model/size/elapsed, mark complete.
  Then the `summarize` step selects the configured **summarize backend** (Task 4),
  passing the slide markdown (empty/whitespace → treated as no slides). Both
  summarize backends write files + publish Notion in one run → mark
  `summarize`+`notion` together.
- Backend selection = pure function of config (`slides.backend`,`summary.backend`).
- `_enabled_stage_names` — pin the tokens: rename the pipeline slide-extraction
  token `slides` → **`scene-extract`**; add **`describe-slides [<backend>]`**
  after `transcribe` when slides on; standardize summarize as
  **`summarize+notion [<backend>]`**.
- `preflight_check`/`_required_env_vars`/`_required_binaries`: require the
  OpenRouter env var iff (slides=openrouter or summary=agno); the Notion token
  env var iff summary=agno; `agy` on PATH iff a stage uses `agy`; surface the
  selected backend per stage in `check` output (R24).

## Constraints
- Failure isolation per recording; a backend failure fails only that recording.
- An empty slide result writes an empty `<name>.slides.md` and still completes.
- The summarize timeout is `timeouts.summarize` or, when unset, `timeouts.agy`.

## Ownership
Owns: `state.py`, `__main__.py`, and their tests. Do not re-implement backend
internals (Tasks 3/4).

## Observable Acceptance
- **Tests (pytest, monkeypatch backends):**
  - Dry-run tokens: `scene-extract` (not `slides`), `describe-slides [<backend>]`,
    `summarize+notion [<backend>]`. Update the existing `"slides" in stages`
    assertion to `"scene-extract"`.
  - `describe-slides` only when slides on; correct backend per config for every
    valid combination; manifest-gated (skipped on retry when complete).
  - Summarize receives slide markdown; no image paths.
  - Pre-flight: OpenRouter env var iff slides=openrouter or summary=agno; Notion
    token env var iff summary=agno; `agy` iff used.
  - Shared-config-distinct-models: slides uses `slides_model`, agno uses
    `summary_model` from the same `openrouter` block.
- **Demo:** `--dry-run` shows the pinned token sequence; a mocked (openrouter,
  agy) run writes `<name>.slides.md` then summarizes via `agy`; a re-run skips
  the paid slides call.
