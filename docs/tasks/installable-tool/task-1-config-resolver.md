# Task 1 — Config auto-discovery resolver

Status: [ ]

Source spec: `docs/specs/installable-tool.md` (R1, R2, R3, R4, R5b; findings P5, E1, E2, E3, E5, E7, E8, P6)

## Objective

Replace the static `--config` default with a precedence-based resolver, and wire it into `main()` with INFO logging of the resolved path.

- Define `ConfigNotFoundError(ConfigError, FileNotFoundError)` in `transcriber/config.py` (E1) and widen `load` to `def load(path: str | Path) -> Config:` (E2).
- Implement `resolve_config_path(cli_config, env, cwd)` in `transcriber/__main__.py` with strict precedence:
  1. explicit `--config PATH` — authoritative; must be an existing regular file (`Path.is_file()`), else raise immediately with **no** CWD fallback.
  2. `TRANSCRIBER_CONFIG` env var — when set and non-empty (after `.strip()`), authoritative with the same regular-file rule and no fallback; empty/whitespace is treated as **unset**.
  3. CWD candidates `transcriber.config.yaml`, then `config.yaml`; if **both** exist, select `transcriber.config.yaml` and log that `config.yaml` is shadowed (P6/R5b).
  4. none → `ConfigNotFoundError` naming all four sources.
- `_build_parser()`: root `--config` default `None`; **`check` subparser `--config` default `argparse.SUPPRESS`** (P5) so a flag before the subcommand is not clobbered. Retire `DEFAULT_CONFIG`.
- `main()`: resolve → `logger.info("using config: %s", config_path)` (E8) → `load(config_path)`. Map `ConfigNotFoundError` and strict missing-file errors to the existing exit-2 config-error path with an actionable message.

## Implementation guidance

- Keep `resolve_config_path` a **pure function** of `(cli_config, env, cwd)` — inject `env: Mapping[str,str]` and `cwd: Path` so tests need no real environment/CWD mutation.
- Use `Path.is_file()` for the regular-file guard so a directory (`--config .`) yields a clear error, not a raw `IsADirectoryError` (E5).
- Preserve exit code 2 for all config-resolution failures (matches the current `except Exception` block around `load`).
- Do not change pipeline/backend behaviour (R10).

## Test requirements (E7 full matrix) — `tests/test_main.py` (or a new `tests/test_config_resolve.py`)

- explicit `--config` wins over env + both CWD candidates (R4).
- **arg ordering:** `--config X check` **and** `check --config X` both resolve `X` (P5 regression — must fail against the naive `default=None` clobber).
- `TRANSCRIBER_CONFIG` used when no `--config`; `TRANSCRIBER_CONFIG=""`/`"  "` treated as unset → CWD candidates.
- non-existent `--config` path → exit 2, no CWD fallback.
- non-existent `TRANSCRIBER_CONFIG` path → exit 2, no CWD fallback.
- directory path via `--config`/`TRANSCRIBER_CONFIG` → clear error (E5).
- both CWD candidates present → `transcriber.config.yaml` chosen **and shadow log emitted** (P6); only `config.yaml` → it is used.
- none present → `ConfigNotFoundError` → exit 2, message names all four sources.
- `main()` emits the `using config:` INFO line on a normal run (E8) — assert via `caplog`.

## Demo

```bash
# tmp dir with only config.yaml
uv run transcriber --dry-run                     # resolves config.yaml
# both candidate files present
uv run transcriber --dry-run                     # transcriber.config.yaml wins, shadow logged
$env:TRANSCRIBER_CONFIG="other.yaml"; uv run transcriber --dry-run   # uses other.yaml
$env:TRANSCRIBER_CONFIG="missing.yaml"; uv run transcriber --dry-run # fails, no fallback
```
