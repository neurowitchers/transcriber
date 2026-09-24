# Task 1 — Config: per-stage backends, `openrouter` section, `notion.token_env`, timeouts

**Status:** [ ]

**Spec:** `docs/specs/slide-description-openrouter.md`
**Dependencies:** none (foundation — Tasks 3/4/5/8 import this model)

## Target
`transcriber/config.py` and `tests/test_config.py`.

## Change
- Replace `Stages.slides: bool` with a nested `SlidesStage`:
  `stages.slides: { enabled: bool, backend: "agy"|"openrouter" }` (backend
  default `"agy"`). **No legacy bool acceptance** — a bare `stages.slides: bool`
  is a `ConfigError`.
- Add `summary.backend: "agy"|"agno"` (default `"agy"`).
- Add an optional `OpenRouter` section, present-or-`None` like `s3`:
  `openrouter: { api_key_env: str, base_url: str = "https://openrouter.ai/api/v1",
  slides_model: str = "google/gemini-2.0-flash-001",
  summary_model: str = "google/gemini-2.5-pro" }`.
- Add `notion.token_env: str | None = None` (env-var NAME for the Notion API key;
  the engine launches the Notion MCP with it).
- Add `timeouts.slides: int = 900` and `timeouts.summarize: int | None = None`
  (falls back to `timeouts.agy` at use time).

### Validation (next to the existing `summary.language` check)
- `slides.backend` ∈ `("agy","openrouter")`; `summary.backend` ∈ `("agy","agno")`
  — else `ConfigError` listing the allowed values for that stage.
- `openrouter` required iff `slides.backend == "openrouter"` **or**
  `summary.backend == "agno"`.
- `notion.token_env` required iff `summary.backend == "agno"`.

## Constraints
- Follow the existing `_build_section` / present-or-`None` pattern (mirror `s3`).
- Secrets are env-var **names** only; never resolve or store secret values here.
- Do not implement any backend/pipeline/orchestration logic — config only.

## Ownership
Owns: `transcriber/config.py` and `tests/test_config.py`. Do not edit
`pipeline.py`, `agent.py`, `__main__.py`, `state.py`, `cleanup.py`, or the
example configs (Task 8 owns examples).

## Observable Acceptance
- **Tests (pytest):**
  - Defaults: omitted backends → both `"agy"`; no `openrouter`/`notion.token_env`
    required.
  - `slides.backend: openrouter` (or `summary.backend: agno`) without
    `openrouter` → `ConfigError`.
  - `summary.backend: agno` without `notion.token_env` → `ConfigError`.
  - Invalid backend value → `ConfigError` (correct allowed set per stage;
    `summary` allows `agno` not `openrouter`, and vice versa).
  - `openrouter` parses `base_url`/`slides_model`/`summary_model` defaults.
  - A bare `stages.slides: true/false` → `ConfigError` (no legacy).
  - `timeouts.slides` defaults 900 / overridable; `timeouts.summarize` default
    `None`.
- **Demo:** load a slides-`openrouter` + summarize-`agno` config and print the
  model; show the missing-`openrouter`, missing-`notion.token_env`, legacy-bool,
  and bad-backend errors.
