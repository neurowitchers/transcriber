# Task 3 — Promote `agno` to a core dependency

Status: [x]

Source spec: `docs/specs/installable-tool.md` (R5, R6; findings P3/X1, E6, E9)

## Objective

Make `summarize` work on a bare `transcriber` install while keeping `transcriber[agno]` resolvable during migration.

- Move `agno[mcp]~=3.0` and `openai~=1.0` from `[project.optional-dependencies].agno` into `[project.dependencies]`.
- Retain a **deprecated no-op `agno = []`** under `[project.optional-dependencies]` with an explanatory comment (P3/X1).
- Re-run `uv lock`.

```toml
[project]
dependencies = [
    "duct", "scenedetect", "av", "PyYAML", "httpx",
    "agno[mcp]~=3.0", "openai~=1.0",   # promoted from the former [agno] extra
]

[project.optional-dependencies]
# Deprecated no-op: agno is now a core dependency. Retained so existing host
# strings `transcriber[agno]` keep resolving during the migration window.
agno = []
```

## Implementation guidance

- Leave the `dev` extra and `[dependency-groups].dev` untouched.
- No behaviour change: `SUMMARY_BACKENDS = ("agno",)` is already the sole summarize backend.
- Independent of the resolver tasks — can run in parallel.

## Test requirements

- `uv run pytest -q` fully green (R6).
- `uv lock --check` resolves cleanly; validated on Python 3.10+ (E9).
- `import transcriber.backends.summarize_agno` succeeds after a **no-extras** `uv sync`.
- `uv sync --extra agno` still succeeds (no-op).
- A test asserting the packaged prompt templates (`transcriber/prompt_templates/*.md`) are present and readable via the agent's template dir, so a build cannot silently drop them (E6).
- `tests/test_examples.py` and any packaging/metadata tests still pass.

## Demo

```bash
uv sync
uv run python -c "import transcriber.backends.summarize_agno"   # succeeds
uv sync --extra agno                                            # no-op success
uv lock --check                                                 # clean
```
