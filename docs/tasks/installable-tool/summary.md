# Task Summary — installable-tool

Source spec: `docs/specs/installable-tool.md`
Critique applied: `docs/critiques/installable-tool.md` (verdict: PROCEED WITH UPDATES)

## Tasks

| # | Task | File | Touches | Depends on |
|---|------|------|---------|------------|
| 1 | Config auto-discovery resolver | `task-1-config-resolver.md` | `transcriber/__main__.py`, `transcriber/config.py`, tests | — |
| 2 | Surface resolved config path in `--dry-run`/`check` | `task-2-surface-config-path.md` | `transcriber/__main__.py`, tests | Task 1 |
| 3 | Promote `agno` to a core dependency | `task-3-agno-core-dependency.md` | `pyproject.toml`, `uv.lock`, tests | — |
| 4 | README + release docs | `task-4-readme-release-docs.md` | `README.md` | Tasks 1–3 |
| 5 | Host migration runbook (docs only) | `task-5-host-migration-runbook.md` | README / `docs/` | Task 4 |

## Parallelization guidance

- **Wave A (parallel):** Task 1 and Task 3 are independent — Task 1 is the CLI resolver (`__main__.py` + `config.py`), Task 3 is `pyproject.toml`/lockfile. They touch disjoint files and can run concurrently in separate subagents.
- **Wave B (after Task 1):** Task 2 extends `main()`/`print_plan`/`_cmd_check` with the resolved path and must build on Task 1's resolver. Keep it with Task 1 (same file `__main__.py`) to avoid a merge seam — ideally the same subagent does Task 1 then Task 2 sequentially.
- **Wave C (after Waves A/B):** Task 4 (README) documents the final behaviour, so it runs after the code tasks land. Task 5 (runbook) follows Task 4.

### Recommended execution

1. Subagent α: Task 1 → Task 2 (sequential, both in `__main__.py`/`config.py`).
2. Subagent β (parallel with α): Task 3 (`pyproject.toml` + lock + tests).
3. After α and β merge and `uv run pytest -q` is green: Task 4, then Task 5 (docs).

## Verification gate

- `uv run pytest -q` must be fully green after Waves A+B.
- `uv lock --check` clean (Task 3).
- Engine PR acceptance = R1–R7, R9, R10. R8/Task 5 is an **external runbook**, not an engine acceptance criterion (critique P1).

## Scope guardrails

- No pipeline/backend/publishing behaviour change beyond config resolution + the dependency promotion (R10).
- Secrets never logged; the resolved **path** only is surfaced/logged.
- Host repos are **not** edited in this engine work — Task 5 only writes the runbook.
