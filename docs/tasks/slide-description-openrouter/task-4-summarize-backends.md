# Task 4 — Summarize backends (agy + agno)

**Status:** [ ]

**Spec:** `docs/specs/slide-description-openrouter.md`
**Dependencies:** Task 1 (config), Task 2 (interfaces + error types). Task 6
provides the Notion-MCP launch constants the `agno` backend calls — coordinate.

## Target
`transcriber/agent.py` (refactor `_slide_block`), `transcriber/backends/summarize.py`
(agy + agno backends; agno in its own optionally-imported module), and
`tests/test_agent.py` / `tests/test_backends.py`.

## Change
- Refactor `agent.py::_slide_block` to accept `slides_markdown: str | None`:
  embed it in a non-XML fence (reuse `_fence_for`) with an instruction to
  incorporate it as the "Slide Descriptions" section when non-empty; return `""`
  when `None`/empty/whitespace. **No image paths** in the prompt anymore. Update
  `build_prompt`/`run_agent` signatures to take `slides_markdown`.
- `AgySummarizeBackend`: today's `run_agent` (writes `<name>.md` +
  `<name>.telegram.md`, publishes Notion via `agy`'s MCP). Behavior unchanged.
- `AgnoSummarizeBackend` (own module, imported only when selected): construct an
  Agno `Agent` with an OpenRouter model (`summary_model`, key/base_url from
  `openrouter`) + the Notion MCP via `MCPTools` (launch constants + token from
  Task 6); run the **same** summary/digest+Notion prompt; write both files and
  publish the Notion subpage. Run async (wrap `asyncio.run`), **bounded by the
  summarize timeout** (`timeouts.summarize` or `timeouts.agy`), closing
  `MCPTools` in a `finally`/context-manager on success/error/**timeout**.
  Post-condition: reuse the non-empty-`<name>.md` success check (empty/absent →
  stage failure). Agent-run atomicity: a Notion failure fails the whole
  `summarize`+`notion` stage (retryable), like `agy`.

## Constraints
- Both backends publish Notion via **MCP** (Spec R11). No hand-rolled Notion REST.
- No silent cross-backend fallback (Spec R19).
- Log hygiene: never log the OpenRouter key, Notion token, or image bytes.

## Ownership
Owns: `agent.py` slide-block refactor, `backends/summarize.py`, the agno module,
and the summarize tests. Coordinate the Notion-MCP launch constants with Task 6.

## Observable Acceptance
- **Tests (pytest):**
  - `_slide_block` embeds markdown when non-empty; omits on `None`/`""`/
    whitespace; no image paths in the prompt.
  - Agy summarize: existing behavior/tests intact.
  - Agno summarize (mock Agno `Agent`/`MCPTools`): model built with
    `summary_model` + resolved key/base_url; Notion MCP attached with the token
    resolved by env-var name; shared prompt run; empty/absent `<name>.md` → stage
    failure; `MCPTools` closed on the exception/timeout path.
  - Log-hygiene: captured logs contain no key/token/image bytes.
- **Demo:** produce a summary via `agy` (mocked bridge) and `agno` (mocked Agno +
  MCP); both write `<name>.md` + `<name>.telegram.md`; show an empty-summary run
  failing.
