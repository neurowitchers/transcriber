# Task 2 — Surface resolved config path in `--dry-run` and `check`

Status: [x]

Source spec: `docs/specs/installable-tool.md` (R3)

## Objective

Make `print_plan(...)` and `_cmd_check(...)` each emit the resolved config path, so an operator can see which file was selected. (Runtime INFO logging on every run is handled in Task 1's `main()` wiring per E8; this task covers the human-facing `--dry-run`/`check` output.)

## Implementation guidance

- Pass the resolved `Path` from `main()` through to `print_plan(...)` and `_cmd_check(...)` (add a parameter).
- Emit the **path only** — never config contents or secrets.
- Add one line near the top of each output block; keep the existing backend-visibility lines (`describe_slides backend=...`, `summarize backend=...`) intact.
- Depends on Task 1 (needs the resolved path). Small, isolated.

## Test requirements — `tests/test_main.py`

- `--dry-run` stdout contains the resolved path; existing plan assertions still hold.
- `check` stdout contains the resolved path on **both** the pass and fail branches.

## Demo

```bash
uv run transcriber --config examples/acme.config.yaml --dry-run   # prints the path
uv run transcriber --config examples/acme.config.yaml check        # prints the path
```
