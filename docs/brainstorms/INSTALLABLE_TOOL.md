# Technical Brainstorm: Convert the Transcriber into a Fully Installable Tool

> **Status:** Phases 1–4 complete (Autoflow). The Phase 2 questions were resolved with the recommended defaults (see answers inline); approaches, comparison, and recommendation follow.

## Problem Statement & Scope

- **Core Objective:** Replace the current git-submodule-plus-hand-maintained-`transcribe.py`-wrapper adoption model with a first-class, installable distribution of the `transcriber` engine, so a host can obtain, pin, upgrade, and run the tool without vendoring source or re-running `uv sync --reinstall-package transcriber`.
- **Scope Boundaries:**
  - **IN:** How the engine is distributed and installed (git ref, private/public index, wheel, `uv tool install`); how a host invokes it (console script vs per-host `transcribe.py`); how the per-host `--config` default is supplied once the wrapper goes away; how pinning and coordinated upgrades across the three known hosts work; migration path off the submodule.
  - **OUT:** Changes to the transcription pipeline itself (STT, slides, summarize, Notion, Telegram, S3 behaviour). The CLI's argument surface and config schema stay as-is except where the wrapper's `--config` injection must be re-homed.
- **Key Constraints:**
  - Python ≥ 3.10, **uv**-managed throughout (per project convention — never plain pip).
  - Must keep working on **Windows** (hosts run `pwsh`); entry-point executables must land on a discoverable `PATH`.
  - **Secrets stay out of config** (env-var *names* only) — any distribution/auth mechanism must not require embedding secrets in committed files.
  - The `agno` extra is **optional-but-required-in-practice** (summarize always runs on `agno`); the install story must make `transcriber[agno]` straightforward.
  - External binaries (**ffmpeg**, and `aws` CLI when S3 is on) remain host prerequisites — a Python install cannot bundle them.
  - Three live hosts already consume the engine as a submodule on branch `enhanced-pipeline`; migration must be non-disruptive and reversible.

## Technical Baseline & External Research

### Current Architecture

- **Package is already installable in principle.** `pyproject.toml` declares a proper build (`hatchling`), a wheel target (`packages = ["transcriber"]`), and a console entry point:
  ```toml
  [project.scripts]
  transcriber = "transcriber.__main__:main"
  ```
  So `uv build` already produces a `transcriber-0.1.0-py3-none-any.whl` whose entry point is `transcriber`. Dependencies: `duct`, `scenedetect`, `av`, `PyYAML`, `httpx`; optional extra `agno = ["agno[mcp]~=3.0", "openai~=1.0"]`; dev extra `pytest`.
- **CLI entry point** is `transcriber/__main__.py:main(argv)`, accepting `[--config PATH] [--keep-intermediates] [--dry-run]` and a `check` subcommand. `DEFAULT_CONFIG = "config.yaml"` (resolved against the current working directory).
- **The actual "script" being converted** is the per-host `transcribe.py` wrapper — it does **not** live in this engine repo. Each host repo carries its own copy. Two observed variants:
  - **scartill-ai-hub / adsight:** wrapper injects a host-specific `--config` default (`transcriber.config.yaml`) when the user supplies none, handling the `check` subcommand's argument ordering, then calls `transcriber.__main__.main`.
  - **flyvercity:** a thinner wrapper that just forwards `sys.argv` and relies on the engine's `config.yaml` default.
- **Current install mechanism (per project factoids):**
  - Engine is a **git submodule** at `./transcriber` in each host (remote `git@github.com:scartill/transcriber.git`, branch `enhanced-pipeline`).
  - Host `pyproject.toml` wires the submodule via `[tool.uv.sources]`; the host runs the **installed** package from `.venv\Lib\site-packages`, *not* the submodule source directly.
  - **Deploy is a documented multi-step ritual:** commit+push engine → `git pull --ff-only` in each host's submodule → **mandatory** `uv sync --reinstall-package transcriber` from each host root → verify by importing `transcriber.agent` / `transcriber.backends`. A submodule checkout alone does **not** take effect. This friction is the core pain the "installable tool" ask targets.
- **Three known hosts** (all branch `enhanced-pipeline`): `scartill/scartill-ai-hub`, `adsight/ai-hub`, `flyvercity/.../local-transcribe`. Each has distinct env-var names, Notion ids, Telegram routing, and config filenames (`transcriber.config.yaml` vs `config.yaml`).

### State of the Art / Industry Standards (uv, verified against docs)

- **`uv tool install`** creates a dedicated, isolated environment for a CLI and drops its entry-point executable on `PATH` (`~/.local/bin`, or `%USERPROFILE%\.local\bin` on Windows; `uv tool dir --bin` prints the exact dir). This is the modern `pipx` replacement for *user-level tools*.
- **Install sources** uv supports for a tool:
  - **Git ref:** `uv tool install "git+ssh://git@github.com/scartill/transcriber.git@<tag|sha|branch>"`. Tags/SHAs pin; branches drift. SSH is convenient on dev machines (agent already configured for `git clone`). This is the lowest-infrastructure path for a private repo — **no index to run**.
  - **Private index** (Artifactory/Nexus/CodeArtifact/GitHub Packages): production-grade pinning + `uv tool install pkg@latest` for coordinated rollout; auth via `UV_INDEX_<NAME>_USERNAME/PASSWORD`.
  - **Pre-built wheel** from a URL or local path (e.g. CI publishes to a bucket): `uv tool install https://.../transcriber-x.y.z-py3-none-any.whl`.
- **Pinning & coordinated upgrade:** `uv tool install 'pkg==1.2.0'` freezes; `uv tool upgrade` is then a no-op until `uv tool install pkg@latest` opts past the pin. For **git** installs there is **no `@latest`** — you reinstall with an explicit new tag/SHA. Extras pass through: `uv tool install 'transcriber[agno]@...'`.
- **`uvx` / `uv tool run`** executes a tool one-off without a persistent install — but the docs note that when a tool must run *against a project* (not our case; our tool runs against a host *config/CWD*, not a Python project) you want `uv run` instead. For transcriber, `uvx transcriber --config ...` is a viable zero-install invocation if the git/index source is reachable.
- **Project-dependency alternative (no `uv tool`):** a host keeps `transcriber` as a normal dependency in its own `pyproject.toml` with `[tool.uv.sources]` pointing at a **git ref** (instead of the submodule path), and invokes `uv run transcriber ...`. This keeps the per-host project model but removes the submodule and the `--reinstall-package` ritual (a `uv lock --upgrade-package transcriber` + `uv sync` picks up a new ref).

### Relevant Ecosystem Options

| Option | Infra needed | Pin/upgrade ergonomics | Windows PATH story | Secrets risk |
| --- | --- | --- | --- | --- |
| Git ref + `uv tool install` | None (just the existing private repo) | Explicit tag/SHA reinstall | Entry point on `%USERPROFILE%\.local\bin` | None (SSH agent) |
| Git ref as host project dep (`uv run transcriber`) | None | `uv lock --upgrade-package` | n/a (runs via `uv run`) | None (SSH agent) |
| Private index (`uv tool install pkg@latest`) | Index server/service | Best (`@latest`, `==`) | Entry point on bin dir | Index creds via env |
| Pre-built wheel (CI → bucket) | CI + artifact store | Version-encoded filename | Entry point on bin dir | Bucket creds |
| PyPI (public) | None beyond PyPI account | Best, public | Entry point on bin dir | Repo becomes public |

## Open Questions & User Clarifications

> **Phase 2 gate — please edit the placeholders below, then tell me to resume.** Your answers determine which 2–4 approaches I draft and compare in Phase 3.

#### Q1: Distribution channel — how public may this be?
- **Context:** The repo is currently private (`git@github.com:scartill/transcriber`). Git-ref installs need zero new infrastructure; a public PyPI release gives the cleanest `uv tool install transcriber` + `@latest` upgrades but makes the package name and releases public. A private index is the middle ground but needs a server.
- **User Input:**
    <!-- USER_INPUT_START:Q1 -->
    **Git ref only** — keep the repo private, stand up no new infrastructure. SSH git-ref installs over the existing `git@github.com:scartill/transcriber.git` remote. (Revisit a private index/PyPI only if the host count grows beyond the three operated by one maintainer.) [Autoflow default A1.]
    <!-- USER_INPUT_END:Q1 -->

#### Q2: Should the per-host `transcribe.py` wrapper disappear entirely, or be auto-generated?
- **Context:** Today the wrapper exists mainly to inject each host's `--config` default and to be invoked via `uv run transcribe.py`. If we move to a console entry point (`transcriber ...` / `uvx transcriber ...`), the host needs another way to supply its config path. Options: (a) always pass `--config` explicitly; (b) a tiny per-host shell/pwsh alias or `.cmd`; (c) engine learns to auto-discover a config by convention (e.g. `transcriber.config.yaml` / `config.yaml` in CWD); (d) an env var like `TRANSCRIBER_CONFIG`.
- **User Input:**
    <!-- USER_INPUT_START:Q2 -->
    **Drop the wrapper; add config auto-discovery (c) + `TRANSCRIBER_CONFIG` (d) as an override.** Precedence: explicit `--config` > `TRANSCRIBER_CONFIG` env var > first existing of `transcriber.config.yaml`, `config.yaml` in CWD > error with a clear message. This covers both observed host conventions with no per-host code. [Autoflow default A2.]
    <!-- USER_INPUT_END:Q2 -->

#### Q3: Who installs/upgrades, and how coordinated must it be?
- **Context:** There are three hosts, all on `enhanced-pipeline`, all operated by you. The current ritual forces a deliberate per-host sync. Do you want (a) each host pinned to an explicit tag you bump deliberately (reproducible, manual), or (b) follow a moving branch for fastest iteration during active development, or (c) `@latest` from an index for one-command fleet upgrades?
- **User Input:**
    <!-- USER_INPUT_START:Q3 -->
    **Follow a branch now (b), add tagged pins later (a).** While `enhanced-pipeline` is under active development, hosts track the branch git ref so a push + per-host `uv lock --upgrade-package transcriber && uv sync` picks up changes. Once the pipeline stabilises, switch hosts to tagged refs for reproducibility. [Autoflow default A3.]
    <!-- USER_INPUT_END:Q3 -->

#### Q4: Should `agno` be a default dependency instead of an optional extra?
- **Context:** `summary.backend` is always `agno` in practice (it is the only summarize backend), yet `agno` is an *optional* extra, so every install must remember `[agno]`. Folding `agno` + `openai` into core `dependencies` removes a footgun at the cost of a heavier install for anyone who (hypothetically) never summarizes.
- **User Input:**
    <!-- USER_INPUT_START:Q4 -->
    **Agno shall be required, no options** (user override of the Autoflow default). Promote `agno[mcp]~=3.0` and `openai~=1.0` from the optional `[agno]` extra into core `[project.dependencies]`. The `[agno]` extra is removed (or kept as an empty alias for one release to avoid breaking any `transcriber[agno]` install string, then dropped). Every install is `transcriber` — summarize works out of the box.
    <!-- USER_INPUT_END:Q4 -->

#### Q5: Versioning & release cadence
- **Context:** Pinning and `@latest` only help if versions move. Currently `version = "0.1.0"` is static. Do you want a tag-driven release flow (bump `pyproject` version → git tag → hosts reinstall from tag), and should it be automated via the existing OpenWiki-style GitHub Actions?
- **User Input:**
    <!-- USER_INPUT_START:Q5 -->
    **Manual tag bumps for now; defer CI automation.** Document a `bump pyproject version → git tag vX.Y.Z → push` release step so the move to tagged pins (Q3 phase two) has versions to point at. A GitHub Actions release workflow is a later, optional hardening step. [Autoflow default A5.]
    <!-- USER_INPUT_END:Q5 -->

### Baseline Assumptions (used if the above are left at defaults)

- **A1:** The repo stays **private**; the lowest-infra path (git-ref `uv tool install` / git-ref host dependency over SSH) is the default working assumption. No new index/bucket is stood up.
- **A2:** The per-host wrapper is replaced by **config auto-discovery in the engine** (search CWD for `transcriber.config.yaml` then `config.yaml`, overridable by `--config` and/or `TRANSCRIBER_CONFIG`), so hosts need neither a vendored script nor a mandatory `--config`.
- **A3:** During active `enhanced-pipeline` development, hosts **follow a branch/SHA**; a tagged-release flow is introduced as a later hardening step.
- **A4 (overridden by user):** `agno` is **promoted to a core dependency** — summarize works on a bare `transcriber` install; the `[agno]` extra is retired.
- **A5:** Versioning remains **manual tag bumps** initially; CI automation is deferred.

---

## Architectural Approaches Evaluated

All approaches share two engine-side changes implied by the resolved answers, independent of distribution channel:

- **Config auto-discovery (Q2):** `__main__._build_parser` currently sets `--config default="config.yaml"` on both the root and `check` parsers. Replace the static default with a resolver: explicit `--config` wins; else `TRANSCRIBER_CONFIG`; else the first existing of `transcriber.config.yaml`, `config.yaml` in CWD; else a clear error naming all three sources. This retires the only real job of the per-host `transcribe.py`.
- **`agno` promoted to core (Q4):** move `agno[mcp]~=3.0` and `openai~=1.0` from `[project.optional-dependencies].agno` into `[project.dependencies]`. The `[agno]` extra is removed. Host dependency strings simplify from `transcriber[agno]` to `transcriber`.

The approaches differ only in **how the engine reaches each host**.

### Approach A: Git-ref dependency in the host project (`uv run transcriber`)

- **Concept:** Delete the `./transcriber` submodule from each host. In the host `pyproject.toml`, keep `transcriber` as a dependency but repoint `[tool.uv.sources]` from `{ path = "transcriber" }` to a git ref: `{ git = "ssh://git@github.com/scartill/transcriber.git", branch = "enhanced-pipeline" }`. Invoke via `uv run transcriber` (the console entry point already exists) from the host root — CWD stays the host root, so config auto-discovery and `recordings_dir: ./recordings` resolve correctly. Upgrades: `uv lock --upgrade-package transcriber && uv sync`.
- **Component Changes:**
  - Engine: config auto-discovery in `__main__.py`; `agno` → core in `pyproject.toml`; README/examples updated; add a `CHANGELOG`/release note. Retire the engine-side `DEFAULT_CONFIG` constant usage.
  - Per host: remove submodule (`git submodule deinit` + `.gitmodules` edit + `git rm`), repoint `[tool.uv.sources]`, delete `transcribe.py`, drop `[agno]` from the dependency string. Document `uv run transcriber`.
- **Dependencies Introduced:** None new (git over existing SSH). Removes the submodule machinery.

### Approach B: User-level `uv tool install` from git ref (`transcriber` on PATH / `uvx`)

- **Concept:** The engine becomes a user-level CLI installed outside any host project: `uv tool install "git+ssh://git@github.com/scartill/transcriber.git@enhanced-pipeline"`. uv builds the wheel and drops a `transcriber` executable on `%USERPROFILE%\.local\bin`. Hosts become pure data directories (config + `recordings/`); you `cd` into a host and run `transcriber` (or `uvx git+ssh://...` for zero-install). Config auto-discovery supplies the per-host config from CWD.
- **Component Changes:**
  - Engine: same two shared changes (auto-discovery, `agno` core). Because `uv tool` installs the *wheel*, confirm `prompt_templates/*.md` ship in the wheel (hatchling includes package data under `transcriber/` by default — verify `slide_extractor.md`, `summary.md` land in the artifact).
  - Per host: remove submodule, remove `transcriber` from host `pyproject.toml` entirely (host no longer needs to be a uv project for transcription), delete `transcribe.py`. Document the one-time `uv tool install` and the upgrade (`uv tool install git+...@<newref>` — git has no `@latest`).
- **Dependencies Introduced:** None new. Shifts from project-local to user-global tool environment.

### Approach C: Private index / PyPI publish (`uv tool install transcriber@latest`)

- **Concept:** Publish versioned wheels to an index (private index, or public PyPI). Hosts install `uv tool install 'transcriber[*]'` or depend on `transcriber>=X` and upgrade with `@latest`. Best fleet-upgrade ergonomics.
- **Component Changes:** Everything in B, **plus** standing up/choosing an index, credentials wiring (`UV_INDEX_*`), and a publish step (manual `uv publish` or CI). Versioning becomes mandatory, not optional.
- **Dependencies Introduced:** Index infrastructure and/or a PyPI release presence (makes the package public if PyPI).

## Structured Comparison & Methodology

### SWOT Matrix

| Approach | Strengths | Weaknesses | Opportunities | Threats/Risks |
|---|---|---|---|---|
| **A. Git-ref host dependency** | Minimal change from today (just swaps `path`→`git` in `[tool.uv.sources]`); keeps the familiar per-host uv-project + `uv run` model; removes submodule and the `--reinstall-package` ritual; fully private, zero new infra; reversible | Host must still be a uv project; upgrade is a 2-command per-host step (`uv lock --upgrade-package` + `uv sync`); no single-command fleet upgrade | Natural later switch to tagged refs (Q3 phase two) with a one-line ref change | Branch refs drift — two hosts synced at different times can run different code (mitigated by tags later) |
| **B. `uv tool install` from git** | Hosts become pure data dirs (no `pyproject.toml` needed); one global `transcriber` on PATH; `uvx` enables zero-install runs; cleanest conceptual "installable tool" | Global tool env can drift from per-host expectations; git installs have **no `@latest`** (must pass new ref); must verify wheel ships `prompt_templates`; Windows PATH setup (`.local\bin`) one-time friction | Trivial to layer an index later (B→C) for `@latest`; good for onboarding a new machine in one command | A stale global install silently serves old code to all hosts; multiple Python/host contexts sharing one tool env |
| **C. Index / PyPI** | Best upgrade UX (`uv tool install transcriber@latest`, `uv tool upgrade --all`); real version pinning; standard distribution | Requires index infra or public release; auth/credential management; mandatory versioning + publish discipline; most work; PyPI makes it public | Scales cleanly past three hosts / multiple maintainers | Over-engineering for a single-maintainer, three-host, private setup; ongoing index maintenance |

**Methodology:** weighted against the resolved constraints — *keep private + zero new infra* (Q1), *fast iteration on `enhanced-pipeline`* (Q3), *single maintainer / three hosts*, *Windows*, and *minimise disruption/reversibility*. Infra cost and iteration speed dominate; fleet-upgrade ergonomics are low-value at three hosts.

## Recommendation

**Adopt Approach A now (git-ref host dependency), with the engine-side auto-discovery + `agno`-core changes, and keep Approach B/C as a documented, low-cost future upgrade.**

Rationale:

- **Smallest, safest delta from today.** The host already depends on `transcriber` via `[tool.uv.sources]`; A only swaps `{ path = "transcriber" }` for `{ git = "...", branch = "enhanced-pipeline" }` and deletes the submodule + wrapper. The `uv run transcriber` invocation and CWD semantics are unchanged, so `recordings_dir: ./recordings` and config discovery behave exactly as the hosts expect. This directly kills the two pain points — the submodule vendoring and the mandatory `uv sync --reinstall-package transcriber` ritual (a plain `uv sync` after `uv lock --upgrade-package transcriber` now suffices).
- **Honours every resolved answer.** Private + zero new infra (Q1), wrapper removed via auto-discovery (Q2), follows the `enhanced-pipeline` branch for fast iteration (Q3), `agno` required in core (Q4), manual tags deferred (Q5).
- **Why not B now:** B is strictly more disruptive (hosts stop being uv projects, global PATH setup, wheel-packaging verification for `prompt_templates`) for a marginal ergonomic gain at three hosts, and git-ref `uv tool` installs still lack `@latest`, so the upgrade story is no better than A during branch-following development. B remains the clean target *after* stabilisation if you want hosts to be pure data dirs — it is a small follow-on, not a rework.
- **Why not C now:** standing up an index or going public contradicts Q1 (private, no new infra) and is over-engineered for one maintainer and three hosts.

### Key Risks & Mitigations

| Risk | Mitigation |
|------|------------|
| **Branch-ref drift** — hosts synced at different times run different engine code (Q3 chose branch-following). | Accept during active dev; record the resolved commit in each host's `uv.lock`. Move to **tagged refs** (Q3 phase two) once `enhanced-pipeline` stabilises — one-line ref change per host. |
| **`agno` promoted to core bloats installs / pulls MCP + `openai` everywhere** (Q4). | Intended per user. Verify `uv lock` resolves cleanly on all three hosts after promotion; confirm no host relied on installing without `agno`. |
| **Config auto-discovery changes default behaviour** — a host that previously relied on the engine's `config.yaml` default, or the wrapper's `transcriber.config.yaml` injection, could pick up the wrong file. | Deterministic precedence (`--config` > `TRANSCRIBER_CONFIG` > `transcriber.config.yaml` > `config.yaml` > error). Cover with unit tests for each branch; surface the resolved path in `--dry-run`/`check` output. |
| **SSH auth unavailable in some context** (e.g. CI) breaks git-ref install. | Hosts are developer machines with SSH agents (per factoids); document an HTTPS+token fallback if CI is added later. |
| **Submodule removal is irreversible-ish / messy in git.** | Standard, documented `deinit`→`.gitmodules` edit→`git rm --cached` sequence; it is reversible via re-add. Do it per host after the engine changes land and are pushed. |
| **Lost `prompt_templates` or data files** if B/C adopted later without packaging them. | When moving to wheel-based install, add an explicit hatchling check/test that `transcriber/prompt_templates/*.md` ship in the built wheel. |

### Summary Table

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Distribution channel | **Git ref over existing private SSH remote** | Zero new infra, stays private (Q1) |
| Install mechanism | **Host-project dependency via `[tool.uv.sources] { git, branch }` + `uv run transcriber`** (Approach A) | Smallest reversible delta from the current submodule model |
| Per-host wrapper | **Deleted; replaced by engine config auto-discovery + `TRANSCRIBER_CONFIG`** | Removes per-host code (Q2) |
| Upgrade flow | **`uv lock --upgrade-package transcriber && uv sync` per host; follow `enhanced-pipeline` branch** | Fast iteration now (Q3); plain `uv sync` replaces the `--reinstall-package` ritual |
| `agno` | **Promoted from extra to core dependency** | User override — summarize always needed (Q4) |
| Versioning | **Manual tag bumps, deferred CI** | Enables Q3 phase-two tagged pins without upfront automation (Q5) |
| Future path | **A → tagged refs → optional B (`uv tool`) / C (index)** | Each step is incremental and non-destructive |

### Phased Execution Plan (ready for `Seed` → full spec)

1. **Engine — config auto-discovery.** Add a resolver in `transcriber/__main__.py` replacing the static `--config default`; precedence `--config` > `TRANSCRIBER_CONFIG` > `transcriber.config.yaml` > `config.yaml` > clear error. Reflect the resolved path in `--dry-run`/`check`. Unit tests per branch.
2. **Engine — `agno` to core.** Move `agno[mcp]~=3.0` + `openai~=1.0` into `[project.dependencies]`; remove the `[agno]` extra. `uv lock`; run the suite (`uv run pytest -q`).
3. **Engine — docs + release note.** Update README adoption section (submodule → git-ref dependency; drop `transcribe.py`; `transcriber` not `transcriber[agno]`); document the manual tag release step; note the auto-discovery precedence and `TRANSCRIBER_CONFIG`.
4. **Engine — commit + push** to `enhanced-pipeline`.
5. **Per host (×3) — migrate off the submodule.** Repoint `[tool.uv.sources]` to the git+branch ref, `git submodule deinit`/remove `./transcriber`, delete `transcribe.py`, drop `[agno]` from the dependency string, `uv lock --upgrade-package transcriber && uv sync`, verify `uv run transcriber --dry-run` resolves the right config and `uv run transcriber check` passes.
6. **Later (deferred): tagged pins (Q3 phase two)**, then optionally **Approach B** (`uv tool install`) once hosts can become pure data dirs.

