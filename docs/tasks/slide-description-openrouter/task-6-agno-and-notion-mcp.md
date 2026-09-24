# Task 6 — Agno dependency + Notion MCP wiring

**Status:** [x]

**Spec:** `docs/specs/slide-description-openrouter.md`
**Dependencies:** Task 1 (config: `notion.token_env`, `openrouter`). Feeds Task 4
(the agno backend calls the launch constants defined here).

## Target
`pyproject.toml` (optional `agno` extra), the Notion-MCP launch module/constants
consumed by `AgnoSummarizeBackend`, and their tests.

## Change
- Add `agno[mcp]` (+ the OpenRouter model provider) as an **optional dependency**
  (an `agno` extra), **version-pinned** (`~=`/exact, not an open range). Import it
  only inside the agno backend; on import failure when `summary.backend == "agno"`
  raise a clear "install the agno extra" error.
- Define engine-owned constants for launching the **official Notion MCP server**
  (package/command + its API-key env-var name). Confirm the exact package + Windows
  command form from Notion's MCP docs during implementation.
- The Notion MCP subprocess env = **`os.environ` merged with**
  `{ <notion mcp token var>: resolve_env(config.notion.token_env) }` — never a
  bare replace (so `PATH`/`APPDATA` survive on Windows).
- **Windows launch:** use a Windows-launchable command form (e.g. `npx.cmd`/shim);
  a bare `npx` frequently fails to spawn. This is the most likely first-run
  blocker.
- Do **not** inspect or reuse `agy`'s MCP configuration — the agno path is
  self-contained, configured only by `notion.token_env` (+ `notion.parent_page_id`
  /`insert`).

## Constraints
- `MCPTools` closed on every path (success/error/timeout) — no orphaned MCP
  subprocess (coordinate the actual close with Task 4's run wrapper).
- Never log the token, key, or headers.

## Ownership
Owns: the `pyproject.toml` `agno` extra + pin, and the Notion-MCP launch
constants/module. Coordinate the call site with Task 4.

## Observable Acceptance
- **Tests (pytest, mock `MCPTools`/Agno `Agent`):**
  - Engine launches the Notion MCP with the API key resolved by env-var name
    (never inlined/logged), passed in the MCP env.
  - The MCP subprocess env is `os.environ` **merged** with the token (a sentinel
    `os.environ` key survives), not a bare replace.
  - `MCPTools` context-exit/`close()` runs on the **exception** path.
  - Missing `agno` import when summary=agno → clear actionable error.
  - No reference to `agy`'s MCP config anywhere in the agno path.
- **Demo:** with Agno + Notion MCP mocked, run an (any, agno) summarize showing
  the MCP launched with the env-merged token and the publish tool invoked.
  **Windows spawn smoke test:** on a Windows host, confirm the real Notion MCP
  command actually spawns before the Task 7 parity run.
