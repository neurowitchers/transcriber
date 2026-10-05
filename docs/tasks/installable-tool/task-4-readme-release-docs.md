# Task 4 — README + release docs

Status: [ ]

Source spec: `docs/specs/installable-tool.md` (R7; findings P2, P4, E4)

## Objective

Rewrite the README adoption model from "git submodule" to "git-ref dependency", and document the new config resolution + release flow.

## Implementation guidance

- Replace the "Adopting as a git submodule" section:
  - Host declares `transcriber` as a dependency (note the no-op `[agno]` extra keeps `transcriber[agno]` resolving) and points `[tool.uv.sources]` at the git ref.
  - Show three ref forms: `branch = "enhanced-pipeline"` (active dev), immutable `tag = "vX.Y.Z"` (preferred once stable, P2), and the HTTPS `https://<token>@github.com/...` form for non-interactive/CI runners without an SSH agent (P4).
  - Invoke via `uv run transcriber`; state that `transcribe.py` is removed.
- Document config auto-discovery: precedence (`--config` > `TRANSCRIBER_CONFIG` > `transcriber.config.yaml` > `config.yaml` > error), the strict-source rule (authoritative, must be a regular file, no fallback; empty env = unset), the shadowing notice, and that **relative config paths resolve against CWD so `transcriber` must run from the host root** (E4).
- Update Prerequisites: drop the submodule step; drop `uv sync --extra agno` (agno is core now).
- Document the manual tag-release step: bump `pyproject` version → `git tag vX.Y.Z` → push.
- Follow the Markdown house rule: no hard-wrapping, one logical line per paragraph. No absolute paths.
- Depends on Tasks 1–3 landing (so the documented behaviour matches the code).

## Test requirements

- n/a (docs). Proofread for internal consistency with the final config precedence, the dependency string, and the ref forms.

## Demo

- README adoption section reads end-to-end with the new install + run flow.
