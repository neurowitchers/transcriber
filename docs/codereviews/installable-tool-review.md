# PR Review: Commit a7e08ce — feat: implement installable-tool config resolver + agno core

**Reviewed**: 2026-10-05
**Author**: Boris Resnick <boris.resnick@gmail.com>
**Branch**: `tool` (commit `a7e08ce` vs parent `f6ccafb`)
**Decision**: APPROVE with comments

## QA & Verification Guidance
- **Risk Assessment & Migration Notes**:
  - The change alters configuration file discovery and elevates `agno[mcp]` and `openai` to core dependencies. It introduces no schema breaks or pipeline modifications.
  - Downstream host repositories (`scartill-ai-hub`, `adsight/ai-hub`, and `flyvercity/local-transcribe`) can safely migrate to a direct git-ref dependency without removing `[agno]` immediately, as a deprecated no-op `agno = []` extra is retained in `pyproject.toml`.
  - When invoking `transcriber` without `--config`, configuration is now auto-discovered from `TRANSCRIBER_CONFIG`, followed by `transcriber.config.yaml`, and then `config.yaml` in the current working directory.
- **Behavioral Changes to Verify**:
  - Root `--config` options supplied before subcommands (e.g. `transcriber --config <path> check`) are no longer overwritten by subparser defaults, behaving identically to `transcriber check --config <path>`.
  - Resolved configuration paths are surfaced in `transcriber --dry-run` and `transcriber check` console output, and logged at `INFO` level during startup of all standard runs.
  - Authoritative sources (`--config` or non-empty `TRANSCRIBER_CONFIG`) pointing to non-existent files or directories fail immediately with exit code 2 and never silently fall back to local working directory candidates.
  - If both `transcriber.config.yaml` and `config.yaml` exist in the working directory, `transcriber.config.yaml` is chosen and an informational message notes the shadowing.
- **Testing Hints for QA**:
  1. In a directory containing both `transcriber.config.yaml` and `config.yaml`, run `uv run transcriber --dry-run` and confirm that `transcriber.config.yaml` is selected with an informational shadow notice in the output.
  2. In an empty directory, execute `uv run transcriber --dry-run` and verify that the command exits with code 2, emitting an error naming `--config`, `TRANSCRIBER_CONFIG`, `transcriber.config.yaml`, and `config.yaml`.
  3. Execute `uv run transcriber --config <path> check` and `uv run transcriber check --config <path>`, confirming both commands locate the file and report `config: <path>` in the output.
  4. Set `TRANSCRIBER_CONFIG="missing.yaml"` and run `uv run transcriber --dry-run`, ensuring immediate termination with exit code 2 and no fallback to working directory candidates.

## Technical Summary
The implementation cleanly fulfills the requirements of `docs/specs/installable-tool.md` and incorporates all critique recommendations from `docs/critiques/installable-tool.md`. The configuration precedence hierarchy, the `argparse.SUPPRESS` fix preventing subparser argument clobbering, the dual-inheritance `ConfigNotFoundError` taxonomy, and the backward-compatible promotion of `agno` to core dependencies are well-architected, robust, and supported by a focused automated test suite.

## Findings

### CRITICAL
None

### HIGH
None

### MEDIUM
- **M1: Relative paths in `--config` and `TRANSCRIBER_CONFIG` bypass injected `cwd` in `resolve_config_path`**:
  In `transcriber/__main__.py`, `_require_regular_file(path: Path, source: str) -> Path` calls `path.is_file()`. When `path` is relative (for example, `config.yaml` or `./configs/custom.yaml`), `path.is_file()` evaluates relative to Python's process working directory (`os.getcwd()`), rather than the injected `cwd: Path` argument of `resolve_config_path`. While standard CLI execution in `main()` passes `Path.cwd()` (where process working directory and `cwd` align), programmatic consumers or tests providing a custom `cwd` alongside a relative CLI or environment path will validate against the process directory instead of `cwd`. Additionally, `resolve_config_path` returns a relative `Path` for explicit CLI/env sources but an absolute `cwd / present[0]` for working directory candidates.
  *Suggestion*: Anchor relative paths against `cwd` before checking existence in `resolve_config_path` (e.g. `target = path if path.is_absolute() else cwd / path`), returning a consistently resolved path.

### LOW
- **L1: Outdated installation instructions in `transcriber/backends/notion_mcp.py`**:
  Following the promotion of `agno[mcp]` to core dependencies in `pyproject.toml`, docstrings and exception messages in `transcriber/backends/notion_mcp.py` (lines 28–30 and 136–139) still advise operators to install the optional extra via `uv sync --extra agno` or `pip install 'transcriber[agno]'`. As `agno` is now an unconditional core dependency, this guidance is deprecated.
  *Suggestion*: Update the `AgnoImportError` message and module documentation in `transcriber/backends/notion_mcp.py` to reflect that `agno` is part of standard dependencies.
- **L2: Missing test coverage for `TRANSCRIBER_CONFIG` directory rejection and failing pre-flight output**:
  While `test_directory_rejected` in `tests/test_config_resolve.py` confirms that directories passed via `--config` raise `ConfigNotFoundError`, the corresponding path for `TRANSCRIBER_CONFIG` pointing to a directory is not explicitly tested. Additionally, while `test_config_flag_before_subcommand_not_clobbered` validates that `check` outputs the configuration path on success, the branch where pre-flight checks fail (which prints `config: <path>` to `sys.stderr`) is unasserted in the test suite.
  *Suggestion*: Expand `tests/test_config_resolve.py` with test cases verifying directory rejection via `TRANSCRIBER_CONFIG` and asserting stderr output during failed pre-flight checks.
- **L3: `tests/test_packaging.py` inspects local repository paths rather than built package distributions**:
  `tests/test_packaging.py` asserts that `agent_mod._TEMPLATES_DIR.is_dir()` and template files exist, which evaluates the repository workspace during editable testing. While this provides a rapid safeguard against moving or deleting prompt templates, it does not confirm that wheel build targets pack the assets.
  *Suggestion*: Retain the fast unit tests and consider introducing a distribution validation step in CI to verify template inclusion in built wheel archives.

## Validation Results

| Check | Result |
|---|---|
| Type check | Pass |
| Lint | Skipped |
| Tests | Pass |
| Build | Pass |

- **Type check**: Python compilation via `python -m py_compile` succeeded cleanly across all modified and newly added files; type annotations in `transcriber/config.py` and `transcriber/__main__.py` match expected signatures.
- **Lint**: No dedicated standalone linter (e.g. `ruff`) is installed in the local virtual environment; manual syntax and code structure inspection verified compliance with conventions.
- **Tests**: `uv run pytest -v` executed 331 tests across the entire suite with 331 passing in 2.03s; dedicated suites `tests/test_config_resolve.py` and `tests/test_packaging.py` passed all 17 tests.
- **Build**: `uv lock --check` resolved all 112 locked packages cleanly; wheel configuration in `pyproject.toml` correctly targets `packages = ["transcriber"]`.

## Files Reviewed
- `pyproject.toml` (Modified)
- `uv.lock` (Modified)
- `transcriber/__main__.py` (Modified)
- `transcriber/config.py` (Modified)
- `tests/test_config_resolve.py` (Added)
- `tests/test_packaging.py` (Added)
