# Task 5 — Host migration runbook (documentation only)

Status: [ ]

Source spec: `docs/specs/installable-tool.md` (R8 — external integration runbook, NOT an engine PR acceptance criterion; finding P7)

## Objective

Capture the exact, repeatable per-host steps to migrate each host off the submodule onto the git-ref dependency. **The actual host edits are executed separately via the deploy factoid — this task only produces the runbook document.**

## Runbook content

For each of the three hosts (scartill-ai-hub, adsight/ai-hub, flyvercity/local-transcribe):

1. Repoint `[tool.uv.sources]` from `{ path = "transcriber" }` to the git ref (branch now, tag once stable).
2. Simplify the dependency string to `transcriber` (the no-op `[agno]` extra keeps `transcriber[agno]` working too).
3. Remove the submodule: `git submodule deinit -f transcriber`, `git rm -f transcriber`, edit `.gitmodules`.
4. Delete `transcribe.py`.
5. `uv lock --upgrade-package transcriber && uv sync` (note: no more `uv sync --reinstall-package transcriber`).
6. **Verify (P7):** `uv run transcriber check` (binaries + env + resolved config) and `uv run transcriber --dry-run` (right config auto-discovered, publishing plan intact).

## Implementation guidance

- PowerShell per project convention (`pwsh -NoProfile -NoLogo`); flag the inline-paren/`[[` parser caveat — use a temp script or `git commit -F msgfile`.
- Note the no-`--reinstall-package` improvement explicitly.
- Place the runbook in the README (or a `docs/` runbook referenced from it).

## Test requirements

- n/a. Runbook is present and self-contained.
