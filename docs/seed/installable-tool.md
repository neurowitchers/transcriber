# Seed: Convert the transcriber into a fully installable tool

## Intent

Today each host repo consumes the engine as a **git submodule** at `./transcriber`, wires it via `[tool.uv.sources] transcriber = { path = "transcriber" }`, and carries a hand-maintained `transcribe.py` wrapper whose only real job is injecting that host's `--config` default. Deploying an engine change is a brittle ritual: push the engine, `git pull` the submodule in each host, then **mandatorily** `uv sync --reinstall-package transcriber` from each host root (the host runs the *installed* package in `.venv`, not the submodule source).

I want to **stop vendoring the engine as a submodule and drop the per-host wrapper**, turning `transcriber` into a tool a host simply *installs* and *runs*. The engine is already installable in principle (it has a `[project.scripts] transcriber = "transcriber.__main__:main"` entry point and a hatchling wheel build) — the work is in the adoption model, not the packaging mechanics.

## What it should do

- A host declares `transcriber` as a normal dependency pointing at the **git repo over SSH** (not a vendored path), and runs the pipeline with `uv run transcriber` from the host root. No submodule, no `transcribe.py`.
- The engine **auto-discovers its config** so no per-host wrapper is needed to inject `--config`.
- Upgrading an engine change on a host becomes `uv lock --upgrade-package transcriber && uv sync` — a plain `uv sync`, replacing the `--reinstall-package transcriber` ritual.
- `summarize` works out of the box on a bare `transcriber` install (no `[agno]` extra to remember).

Everything about the transcription pipeline itself (STT, slides, summarize, Notion, Telegram, S3) is unchanged except for the config-resolution entry point.

## Key decisions (from brainstorm)

- **Recommended solution: Approach A — git-ref host dependency.** In each host `pyproject.toml`, repoint `[tool.uv.sources]` from `transcriber = { path = "transcriber" }` to `transcriber = { git = "ssh://git@github.com/scartill/transcriber.git", branch = "enhanced-pipeline" }`, delete the `./transcriber` submodule, and invoke via `uv run transcriber`. CWD stays the host root, so `recordings_dir: ./recordings` and config auto-discovery resolve exactly as before. (Approaches B `uv tool install` and C index/PyPI were evaluated and deferred as incremental, non-destructive future steps.)
- **Distribution channel: git ref only, over the existing private SSH remote.** Keep the repo private; stand up **no new infrastructure** (no index, no bucket, no PyPI). Revisit only if the host count grows beyond the three single-maintainer hosts.
- **Drop the `transcribe.py` wrapper; add config auto-discovery to the engine.** Resolution precedence: explicit `--config` > `TRANSCRIBER_CONFIG` env var > first existing of `transcriber.config.yaml`, then `config.yaml` in CWD > a clear error naming all sources. This covers both observed host conventions (`transcriber.config.yaml` and `config.yaml`) with no per-host code. The resolved path is surfaced in `--dry-run`/`check` output.
- **`agno` is required, no options** (user override). Promote `agno[mcp]~=3.0` and `openai~=1.0` from the optional `[agno]` extra into core `[project.dependencies]`; remove the `[agno]` extra. Host dependency strings simplify from `transcriber[agno]` to `transcriber`.
- **Follow the branch now, tag later.** While `enhanced-pipeline` is under active development, hosts track the branch git ref (fast iteration). Once the pipeline stabilises, switch hosts to **tagged refs** for reproducibility — a one-line ref change per host.
- **Versioning: manual tag bumps, CI deferred.** Document a `bump pyproject version → git tag vX.Y.Z → push` release step so the later move to tagged pins has versions to point at. A GitHub Actions release workflow is an optional later hardening step.

## Config / packaging changes (illustrative, not final)

Engine `pyproject.toml` — promote `agno` to core:

```toml
[project]
dependencies = [
    "duct",
    "scenedetect",
    "av",
    "PyYAML",
    "httpx",
    "agno[mcp]~=3.0",   # promoted from the removed [agno] extra
    "openai~=1.0",      # promoted from the removed [agno] extra
]
# [project.optional-dependencies].agno is removed.
```

Host `pyproject.toml` — git ref instead of submodule path:

```toml
[project]
dependencies = [
    "transcriber",      # was transcriber[agno]
]

[tool.uv.sources]
transcriber = { git = "ssh://git@github.com/scartill/transcriber.git", branch = "enhanced-pipeline" }
```

Config resolution (replaces the static `--config default="config.yaml"` on both the root and `check` parsers in `transcriber/__main__.py`):

```
--config PATH            # explicit, wins
TRANSCRIBER_CONFIG=...    # env var, second
./transcriber.config.yaml # auto-discover, third
./config.yaml             # auto-discover, fourth
→ else: clear error listing all four sources
```

## Follow-up tasks

- **Engine — config auto-discovery:** add the resolver in `transcriber/__main__.py` (`_build_parser` currently hard-codes `default=DEFAULT_CONFIG` on the root and `check` parsers); reflect the resolved path in `--dry-run`/`check`; unit-test each precedence branch, including the no-config error.
- **Engine — `agno` to core:** move `agno[mcp]~=3.0` + `openai~=1.0` into `[project.dependencies]`, remove the `[agno]` extra, re-`uv lock`, run `uv run pytest -q`.
- **Engine — docs:** rewrite the README "Adopting as a git submodule" section (→ git-ref dependency; drop `transcribe.py`; `transcriber` not `transcriber[agno]`), document the auto-discovery precedence + `TRANSCRIBER_CONFIG`, and add the manual tag-release step. Update prerequisites (no submodule; `agno` always installed).
- **Hosts (×3: scartill-ai-hub, adsight/ai-hub, flyvercity/local-transcribe):** repoint `[tool.uv.sources]` to the git+branch ref, `git submodule deinit` + remove `./transcriber` + edit `.gitmodules`, delete `transcribe.py`, drop `[agno]` from the dependency string, `uv lock --upgrade-package transcriber && uv sync`, verify `uv run transcriber --dry-run` resolves the right config and `uv run transcriber check` passes.
- **Later (deferred):** switch hosts to tagged refs (Q3 phase two); optionally adopt Approach B (`uv tool install`) once hosts can become pure data dirs.

## Risks to carry into the full spec

- **Branch-ref drift** — hosts synced at different times run different engine code (an accepted consequence of branch-following during active dev). Record the resolved commit in each host's `uv.lock`; move to tagged refs once stable.
- **Config auto-discovery changes default behaviour** — a host that previously relied on the engine's `config.yaml` default or the wrapper's `transcriber.config.yaml` injection could pick up the wrong file. Keep precedence deterministic, cover every branch with tests, and surface the resolved path in `--dry-run`/`check`.
- **`agno` in core bloats installs** (pulls MCP + `openai` everywhere) — intended per the user override; verify `uv lock` resolves cleanly on all three hosts and that no host depended on installing without `agno`.
- **SSH auth unavailable in some context** (e.g. a future CI) breaks git-ref installs — hosts are developer machines with SSH agents today; document an HTTPS+token fallback if CI is added.
- **Submodule removal is messy in git** — follow the standard `deinit` → `.gitmodules` edit → `git rm --cached` sequence; it is reversible via re-add. Do it per host only after the engine changes land on `enhanced-pipeline`.
- **Packaging gap if B/C adopted later** — a wheel-based install must ship `transcriber/prompt_templates/*.md`; add an explicit packaging test when that step is taken (not needed for Approach A, which installs from source build).
