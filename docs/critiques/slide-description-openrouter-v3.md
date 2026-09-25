# Critique: Two Pluggable Post-Transcript Stages (Slide Description + Summarize) with Selectable Backends

**Spec under review:** `docs/specs/slide-description-openrouter.md`
**Date:** 2026-09-25
**Reviewer lenses:** Product (CEO/Product Lead) + Engineering (Staff Engineer)
**Note:** Two prior critiques exist. `slide-description-openrouter.md` reviewed
an early single-stage version. `slide-description-openrouter-v2.md` reviewed the
two-stage version and raised the scenes-CSV location gap. This v3 report reviews
the **current, further-evolved** spec, in which the scenes-CSV gap (E7), the
engine-owned Notion MCP decision (R11), the deterministic payload ceiling (R23),
and the operator-visibility requirement (R24) have all been folded in. The focus
here is on what remains open in the current text.

---

## Executive Summary

**Overall assessment: strong, implementation-ready spec with a small set of
must-address clarifications and a few recommendations.**

The spec is well-grounded; its background claims were re-verified against the
code and match. The two-stage split (`describe_slides` / `summarize`) with
orthogonal per-stage backends is clean and additive, with an honest rollback
story (both stages on `agy` = today). Requirements are numbered and testable;
tasks carry explicit demos and test requirements. The prior critique's main
engineering gap (scenes-CSV location) has been correctly absorbed into R8/E7 and
Tasks 3/7, with the real-host confirmation that the CSV lives in the slides dir.

Verification performed against the codebase for this review:

- `config.py` — confirmed `Stages(slides: bool, s3_sync: bool)`,
  `Summary(language, sections)`, `Timeouts` (fields ffmpeg/scenedetect/
  elevenlabs/agy/s3, **no `slides` yet**), `S3` optional via
  `data.get("s3")`, `_build_section`/`_has_default`, `resolve_env`,
  single `summary.language` validation point. Matches the spec's stated shape.
- `agent.py` — confirmed `_slide_block(slide_image_paths)` (currently takes
  **image paths**, embeds `slide_extractor.md`), `build_prompt(...)` with
  `{slide_block}`, `_transcript_fence`/`_fence_for`, `run_agent(...)` driving
  `agy` with the non-empty-summary post-condition and `AgyTimeoutError` partial
  handling. Matches.
- `pipeline.py` — confirmed `_extract_slides` passes a **relative**
  `scenes_csv = f"{name}.scenes.csv"` together with `-o str(slides_dir)`, so
  scenedetect writes the CSV **inside the slides dir**. This confirms E7.
- `__main__.py` — confirmed `_process_one` order
  (pipeline → summarize+notion → telegram → s3 → cleanup),
  `_slide_image_paths`, `_transcript_path`, `_enabled_stage_names`,
  `preflight_check`, `_required_env_vars` (**Telegram token only** today),
  `_required_binaries` (ffmpeg/elevenlabs/agy always; scenedetect iff slides;
  aws iff s3).
- `state.py` — confirmed `STAGES = (pipeline, summarize, notion, telegram, s3,
  cleanup)`, `mark_complete` persists immediately, `is_complete` uses
  `_completed.get(stage, False)`, and `_load` filters `if stage in STAGES`.
- `cleanup.py` — confirmed `INTERMEDIATE_SUFFIXES = (".mp3",".txt",".scenes.csv")`,
  `KEEP_SUFFIXES = (".mp4",".md")`, `intermediate_paths` lists
  `directory / f"{name}.scenes.csv"` (**recording dir — the wrong place, per
  E7**) plus `<name>.telegram.md` and the slides dir, and `_has_keep_suffix`
  special-cases `.telegram.md`.

The verification surfaced no new fundamental flaws. The remaining items are
consistency gaps between the spec's prose and the code it will touch, plus a few
robustness/observability refinements.

**Verdict: ⚠️ PROCEED WITH UPDATES.**

---

## Product Lens Findings

### 1a. Problem Validation

- The problem is concrete and real: one heavy `agy` run couples vision +
  summarize + publish, with no per-piece cost control. Confirmed by the code
  (`_process_one` runs a single `run_agent` that does all three).
- **P1 (💡) — ROI premise is measured late.** The 40% wall-time win is the
  Task 7 pass/fail bar, but the baseline is captured at the *P1 checkpoint
  before Task 2*, i.e. after Task 1 is already built. Since Task 1 is cheap and
  reversible this is acceptable, but the spec should state what happens to the
  already-merged Task 1 config surface if the checkpoint fails the 40% bar
  (keep it dormant vs. revert). Suggestion: add one sentence — "if the checkpoint
  fails, Task 1's additive config stays (inert, defaults to `agy`) and Tasks 2–7
  are shelved."

### 1b. User Value Assessment

- The primary cost-saver config (slides `openrouter` + summarize `agy`) delivers
  value without the heavier `agno` dependency, and rollback is a config flip.
  Good MVP framing.
- **P2 (🤔) — the "operator" is the only user; success is manual.** The pass/fail
  bar (Task 7) is a *single* manual parity check on *one* representative
  recording. That's a reasonable smoke test but a weak generalization for a
  cost/quality claim. Suggestion: either (a) explicitly scope the 40% claim to
  "the baseline recording, not a guarantee across recordings," or (b) run the
  parity check on 2–3 recordings of differing slide counts. Cheap insurance
  against a one-off favorable sample.

### 1c. Alternative Approaches

- Alternatives (global cheap toggle; per-stage model to `agy`) are considered
  and rejected with reasons that hold up against the code (`agy`'s model isn't
  per-stage configurable from this engine). D1/D3 are resolved. Good.
- **P3 (💡) — "bare OpenRouter for summarize is impossible" deserves a hedge.**
  R11/D1 assert a plain chat-completions call "cannot invoke MCP tools." True for
  a raw call, but the underlying reason is really "we refuse to hand-roll a Notion
  REST client." Consider stating it as a *policy* choice ("we will not maintain a
  bespoke Notion REST client; MCP is the single Notion integration path") rather
  than a hard technical impossibility, so the rationale survives if someone later
  asks "why not just call the Notion API directly for a summary blob?"

### 1d. Edge Cases & UX

- Empty-slides semantics (R9) and slides-off (R2/R3) are handled coherently:
  empty result → empty `<name>.slides.md` → block omitted. Consistent with the
  current `_slide_block("") -> ""` behavior.
- **P4 (💡) — discoverability of the backend mix.** R24 adds backend visibility
  to `--dry-run`/`check`, which is good. But `_enabled_stage_names` today emits a
  single `"summarize+notion"` token, while the spec's Task 5 sometimes writes
  `"summarize"` and sometimes `"summarize+notion"`. Pick one label and keep the
  `+notion` suffix (it communicates the atomicity that R20/R21 rely on). See E1.

### 1e. Success Measurement

- Success criteria are concrete (≥40% wall-time, no visible quality regression).
- **P5 (🤔) — cost vs. wall-time.** The success bar is *wall-time*, but the
  motivation is *cost*. On a cheap vision model, wall-time and $-cost usually
  move together, but not always (a slow-but-cheap model could fail the 40%
  wall-time bar while still winning on cost). Suggestion: capture token/$-cost at
  the P1 baseline "if the bridge surfaces it" (already noted) and record it in
  Task 7 as a secondary metric, so a wall-time miss with a large cost win is a
  conscious decision rather than an automatic fail.

---

## Engineering Lens Findings

### 2a. Architecture Soundness

- The backend-interface split (`SlidesBackend` / `SummarizeBackend` Protocols)
  with a pure config→backend selection function is clean and testable, and keeps
  `pipeline.process_recording` network-free. Good separation.
- **E1 (🎯) — stage-naming / manifest inconsistency must be pinned down.** The
  current `state.STAGES` has `summarize` and `notion` as **separate** entries,
  and `_process_one` marks both together. The spec (R20) says to insert
  `describe_slides` and keep marking `summarize`+`notion` together. But two
  concrete details are underspecified and will bite the implementer:
  1. **Insertion position vs. `_load` filtering.** `state._load` keeps only
     `stage in STAGES`, and `is_complete` defaults missing keys to `False`, so
     old manifests correctly treat `describe_slides` as incomplete (R20 is
     right). Confirm the new tuple is exactly
     `(pipeline, describe_slides, summarize, notion, telegram, s3, cleanup)` and
     update the `state.py` docstring's "Tracked stages" list — the spec says to
     but doesn't show the final tuple. Add the literal tuple to Task 5.
  2. **Dry-run label.** `_enabled_stage_names` emits `audio-extract`, `slides`,
     `transcribe`, `summarize+notion`, `telegram`, `s3-sync`. The spec's Task 5
     says add `describe-slides` "after transcribe" and "keep `summarize`", but
     the code token is `summarize+notion`, and it also emits a `slides` token
     for the *pipeline* slide-extraction step. There are now **two** slide-ish
     concepts (pipeline scene-extraction vs. the new `describe_slides` stage).
     Suggestion: rename the pipeline token to `scene-extract` (or `extract-slides`)
     and use `describe-slides [backend]` for the new stage, so the plan doesn't
     show two ambiguous "slides" lines. Pin the exact tokens in Task 5.

### 2b. Failure Mode Analysis

- Retry/backoff for `429`/`5xx` (R15) and a deterministic pre-send payload
  ceiling (R23) are specified — good, and the ceiling correctly makes `413` a
  local `SlideDescribeError` rather than a provider round-trip.
- **E2 (💡) — Agno/MCP failure taxonomy is thinner than the OpenRouter one.**
  R15/R23 give the slides call a concrete resilience story. The `agno` summarize
  path (R21/R22) only says "a failure fails the stage (retryable)" and "empty
  file fails." But an MCP `connect()` hang, a stalled agent turn, or an MCP
  subprocess that never exits are distinct failure modes with no bound. The
  `agy` path ties its bound to `timeouts.agy` + `idle_timeout`; the `agno` path
  should state an equivalent hard bound. Suggestion: wrap the `asyncio.run`
  in a timeout equal to `timeouts.agy` (which R13 already reuses as the
  summarize-stage timeout) and ensure `MCPTools` is closed in a `finally`/context
  manager even on timeout, so a hung MCP subprocess is reaped. Add this to R22 /
  Task 4.
- **E3 (💡) — no explicit MCP-subprocess cleanup on crash.** The engine launches
  the Notion MCP server (`MCPTools(command=...)`). If the parent raises between
  `connect()` and `close()`, an orphaned `npx`/node process can linger. The
  sketch uses `async with MCPTools(...)`, which handles the happy and exception
  paths — good — but Task 6's tests should assert `close()`/context-exit runs on
  the *exception* path too, not only success.

### 2c. Security & Privacy

- Secret-by-name (R12), no secrets in config/logs (R16/R17), and the explicit
  decision that the `agno` path launches its **own** Notion MCP from
  `notion.token_env` rather than reverse-engineering `agy`'s MCP config (R11) —
  all sound and consistent with the existing `resolve_env` pattern.
- **E4 (🎯) — the Notion token now flows through a spawned subprocess env; the
  spec must nail the mechanism.** The sketch passes
  `env={NOTION_MCP_TOKEN_ENV: notion_token}` to `MCPTools`. Two must-address
  details:
  1. **Env inheritance.** If `MCPTools`/the MCP launcher does *not* inherit the
     parent environment when `env=` is supplied, the `npx`-spawned server may
     lose `PATH`/`APPDATA` and fail to start on Windows (this engine is
     Windows-first per `pipeline.py`). The spec should say whether `env` is
     merged over `os.environ` or replaces it, and default to merge.
  2. **Windows `npx`.** `command="npx -y @notionhq/notion-mcp-server"` is a
     string; on Windows the launcher may need `npx.cmd` or `shell=True`
     semantics. Task 6 already says "confirm the exact package/env-var from
     Notion's docs" — extend that to "confirm the Windows-launchable command
     form," since a bare `npx` frequently fails to spawn on Windows without the
     `.cmd` shim. This is a likely first-run blocker, hence 🎯.
- **E5 (💡) — image downscale is a privacy control, not just a size control.**
  R16 documents image egress; R23/the sketch downscale to a bounded long edge.
  Worth stating explicitly that downscaling also *reduces* (not eliminates) the
  fidelity of any incidentally-captured sensitive on-slide content, and that it
  is **not** a redaction mechanism — so operators don't over-trust it.

### 2d. Performance & Scalability

- Single-request vision call (bounded cost/latency, preserves cross-slide dedup)
  is a reasonable default, and the payload ceiling caps the worst case.
- **E6 (🤔) — single-call ceiling vs. long decks.** R23 raises
  `SlideDescribeError` when a deck exceeds the max-slides / max-bytes ceiling
  *before sending*. For a genuinely long lecture (many scene cuts), that means
  the `openrouter` slides backend simply **fails** rather than degrading (e.g.
  chunking into multiple calls, or truncating with a warning). Is a hard failure
  the intended behavior, or should over-ceiling decks fall back to `agy` /
  chunk? The spec explicitly forbids silent backend fallback (R19), so "fail" is
  internally consistent — but confirm that's the *desired* operator experience
  for long recordings, or the primary cost-saver config will hard-fail on
  exactly the long meetings where it matters most. Needs a product/eng decision;
  document it in R23.

### 2e. Testing Strategy

- Unit coverage is thorough and mock-based (httpx for OpenRouter; Agno `Agent`/
  `MCPTools` mocked; `run_agent` mocked). Manifest-gating, backend selection,
  pre-flight matrices, and cleanup are all covered. Strong.
- **E7 (💡) — no test asserts the two OpenRouter consumers share one config.**
  R13 says the slides vision call and the `agno` model share the `openrouter`
  section (`api_key_env`/`base_url`) but use **different** models
  (`slides_model` vs `summary_model`). Add a test that the slides backend uses
  `slides_model` and the `agno` backend uses `summary_model` from the *same*
  `openrouter` block, so a future refactor can't cross-wire them (e.g. summarize
  accidentally sending the cheap vision model to MCP tool-calling, which R13
  warns is unreliable).
- **E8 (💡) — assert secrets never appear in logs.** R16/R17 require it; Task 4/6
  mention it. Add an explicit test that captures log output during a mocked
  `agno`/openrouter run and asserts the resolved key/token and image bytes are
  absent. "Never logged" is only real if a test enforces it.

### 2f. Operational Readiness

- Observability (R17: stage/model/size/elapsed at INFO; MCP tool
  invocation logged) and rollback (both stages → `agy`) are defined.
- **E9 (💡) — `timeouts.agy` doing double duty is a latent footgun.** R13 reuses
  `timeouts.agy` as the summarize-stage timeout "regardless of backend." That's
  pragmatic, but an operator who sets `timeouts.agy` low for a fast local agent
  will unknowingly starve a slower `agno`+MCP run (network + tool round-trips).
  Suggestion: keep the reuse as the default but note it in the README, or add an
  optional `timeouts.summarize` that falls back to `timeouts.agy` when unset.
  Low urgency, but it will confuse operators mixing backends.

### 2g. Dependencies & Integration Risks

- `httpx` is already declared; `agno[mcp]` is correctly scoped as an optional
  extra imported only when `summary.backend == "agno"`, with a clear
  install-me error on missing import. Good containment of a heavy dependency.
- **E10 (🤔) — Agno + OpenRouter model-provider API is assumed, not pinned.** The
  sketch uses `agno.models.openrouter.OpenRouter` and `agno.tools.mcp.MCPTools`
  with `arun`. Agno's API surface moves; the exact import paths, the `MCPTools`
  `command`/`env` signature, and whether `Agent.arun` is the right entry point
  should be confirmed against the *pinned* Agno version during Task 6, and that
  version should be pinned in `pyproject.toml` (exact/`~=`) rather than an open
  range, so a minor Agno release can't silently break the summarize path.

---

## Cross-Lens Insights

- **X1 (🎯) — the "slides" naming collision is both a UX and an engineering
  clarity problem (P4 × E1).** Today `_enabled_stage_names` emits `slides` for
  the *pipeline* scene-extraction step; the new stage is also about slides
  (`describe_slides`). Operators reading `--dry-run` and implementers editing
  `state.STAGES`/`_enabled_stage_names` will both benefit from disambiguating the
  tokens (`scene-extract` for pipeline extraction vs. `describe-slides [backend]`
  for the new stage). Resolving this once fixes both the operator-visibility
  requirement (R24) and the manifest/plan consistency (R20). Highest-value single
  edit.
- **X2 (💡) — Windows-first launch of the Notion MCP (E4 × operational
  readiness).** The engine is Windows-first, and the single riskiest *new*
  runtime dependency is spawning the Notion MCP server via `npx` on Windows with
  a scrubbed env. This is where the `agno` path is most likely to fail on a real
  host. Front-load a Windows spawn smoke test in Task 6 rather than discovering
  it during the Task 7 parity run.
- **X3 (💡) — long-deck failure mode (P2/E6).** The success metric is measured on
  one recording, and the single-call ceiling hard-fails long decks. If the
  parity recording happens to be short, the ceiling behavior on long decks ships
  untested. Pick a parity recording near/above the expected slide-count ceiling,
  or add a unit test for the over-ceiling path (the latter is already in Task 3's
  test list — good; just ensure the *product* decision in E6 is made first).

---

## Findings Summary Table

| ID | Lens | Severity | Category | Finding | Suggestion |
|----|------|----------|----------|---------|------------|
| P1 | Product | 💡 | Problem Validation | ROI baseline captured after Task 1 is merged; no stated disposition if 40% bar fails | Add a sentence: failed checkpoint → Task 1 config stays inert (defaults `agy`), Tasks 2–7 shelved |
| P2 | Product | 🤔 | User Value | 40% claim rests on one manual parity check on one recording | Scope the claim to the baseline recording, or check 2–3 recordings of differing slide counts |
| P3 | Product | 💡 | Alternatives | "Bare API cannot summarize" framed as technical impossibility | Reframe R11/D1 as a policy: no hand-rolled Notion REST client; MCP is the sole Notion path |
| P4 | Product | 💡 | Edge Cases/UX | Dry-run backend label inconsistent (`summarize` vs `summarize+notion`) | Standardize on `summarize+notion [backend]`; keep `+notion` to signal atomicity |
| P5 | Product | 🤔 | Success Measurement | Success bar is wall-time but motivation is cost | Record token/$-cost as a secondary Task 7 metric; allow a conscious wall-time-miss/cost-win call |
| E1 | Engineering | 🎯 | Architecture | Final `STAGES` tuple and dry-run tokens not pinned; two ambiguous "slides" concepts | Add literal `(pipeline, describe_slides, summarize, notion, telegram, s3, cleanup)`; disambiguate tokens |
| E2 | Engineering | 💡 | Failure Modes | `agno` path lacks a hard time bound like `agy`'s | Wrap `asyncio.run` in `timeouts.agy`; close `MCPTools` in finally even on timeout |
| E3 | Engineering | 💡 | Failure Modes | No asserted MCP-subprocess cleanup on exception path | Task 6 test: assert context-exit/`close()` runs when the agent run raises |
| E4 | Engineering | 🎯 | Security/Integration | Token flows via spawned MCP env; env-merge + Windows `npx` spawn unspecified | Specify env merged over `os.environ`; confirm Windows-launchable command (`npx.cmd`/shim) in Task 6 |
| E5 | Engineering | 💡 | Privacy | Downscale could be mistaken for redaction | State downscaling reduces but does not redact sensitive on-slide content |
| E6 | Engineering | 🤔 | Performance | Over-ceiling long decks hard-fail (no chunk/fallback) | Confirm hard-fail is the intended operator experience for long recordings; document in R23 |
| E7 | Engineering | 💡 | Testing | No test that the two OpenRouter consumers share one config but distinct models | Add a test: slides uses `slides_model`, agno uses `summary_model`, same `openrouter` block |
| E8 | Engineering | 💡 | Testing | "Secrets never logged" not enforced by a test | Add a log-capture test asserting key/token/image bytes are absent |
| E9 | Engineering | 💡 | Operational | `timeouts.agy` reused for `agno` can starve a slower networked run | Note in README, or add optional `timeouts.summarize` falling back to `timeouts.agy` |
| E10 | Engineering | 🤔 | Dependencies | Agno API surface assumed, not pinned | Confirm import paths/`arun` against a pinned Agno version; pin exact/`~=` in `pyproject.toml` |
| X1 | Both | 🎯 | UX × Architecture | "slides" naming collision (pipeline extract vs describe_slides) | Rename pipeline token to `scene-extract`; use `describe-slides [backend]` |
| X2 | Both | 💡 | Ops × Integration | Windows `npx` MCP spawn is the riskiest new runtime dependency | Front-load a Windows spawn smoke test in Task 6 |
| X3 | Both | 💡 | Product × Perf | Ceiling behavior on long decks may ship untested | Pick a parity recording near the ceiling, and resolve E6 first |

---

## Verdict

⚠️ **PROCEED WITH UPDATES.** The architecture is sound and the spec is close to
implementation-ready. Three 🎯 must-address items are all resolvable with spec
edits (no redesign):

- **E1 / X1** — pin the final `STAGES` tuple and disambiguate the dry-run
  stage tokens (two "slides" concepts).
- **E4** — specify Notion-MCP env inheritance (merge over `os.environ`) and
  confirm a Windows-launchable `npx` command form in Task 6.

The 🤔 questions (P2, P5, E6, E10) need a product/owner decision but do not block
starting Task 1 (config), which is additive and reversible.

---

## Remediation

Suggested spec edits for the must-address items:

- **E1 / X1 (Task 5):** add the literal target tuple
  `STAGES = (pipeline, describe_slides, summarize, notion, telegram, s3, cleanup)`
  and pin the dry-run tokens: rename the pipeline slide-extraction token to
  `scene-extract`, and emit `describe-slides [<backend>]` and
  `summarize+notion [<backend>]` for the new/updated stages.
- **E4 (R11 / Task 6):** add "The Notion MCP subprocess environment is
  `os.environ` **merged** with `{token_env: resolved_key}` (never a bare replace,
  so `PATH`/`APPDATA` survive on Windows). Confirm the Windows-launchable command
  form for the official Notion MCP server (e.g. `npx.cmd`), as a bare `npx`
  frequently fails to spawn on Windows."
- **E2 (R22 / Task 4):** add "The `agno` run is bounded by `timeouts.agy`;
  `MCPTools` is closed in a `finally`/context-manager even on timeout or error."

Would you like me to apply these changes to the spec? (all / select / none)
After applying, re-run `Critique` to verify.
