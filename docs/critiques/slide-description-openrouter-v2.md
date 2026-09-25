# Critique: Two Pluggable Post-Transcript Stages (Slide Description + Summarize) with Selectable Backends

**Spec under review:** `docs/specs/slide-description-openrouter.md`
**Date:** 2026-09-24
**Reviewer lenses:** Product (CEO/Product Lead) + Engineering (Staff Engineer)
**Note:** A prior critique (`docs/critiques/slide-description-openrouter.md`)
reviewed an earlier, narrower version of this spec (single new stage). This
report reviews the current, evolved spec (two stages, per-stage backends,
engine-owned Notion decision).

---

## Executive Summary

**Overall assessment: strong spec, ready to proceed with a small set of
must-address clarifications.**

This spec is well-grounded and its background claims largely match the code.
The two-stage split (`describe_slides` / `summarize`) with orthogonal per-stage
backends is a clean, additive design with an honest rollback story (both stages
on `agy` = today). Requirements are numbered, testable, and the task breakdown
is sequenced sensibly with explicit demos and test requirements.

Verification performed against the codebase:

- `config.py` — confirmed `Stages.slides: bool`, `Summary(language, sections)`,
  `Timeouts` (no `slides` field yet), `S3` optional-section pattern,
  `resolve_env`. Matches the spec's stated additions.
- `agent.py` — confirmed `_slide_block(slide_image_paths)`, `build_prompt(...)`
  with `{slide_block}`, `_transcript_fence`/`_fence_for`, and
  `run_agent(...)` driving `agy` (writes `<name>.md` + `<name>.telegram.md`,
  Notion via MCP prompt). Matches.
- `pipeline.py` — confirmed ffmpeg → scenedetect → transcribe, `_run_command`
  seam, idempotent stages.
- `__main__.py` — confirmed `_process_one` stage order, `_slide_image_paths`,
  `_transcript_path`, `_enabled_stage_names`, `preflight_check`,
  `_required_env_vars`, `_required_binaries`.
- `state.py` — confirmed `STAGES = (pipeline, summarize, notion, telegram, s3,
  cleanup)` and `mark_complete` persisting immediately.
- `cleanup.py` — confirmed `INTERMEDIATE_SUFFIXES`, `KEEP_SUFFIXES`,
  `_has_keep_suffix` special-casing `.telegram.md`.

The verification surfaced **one concrete engineering gap** (the location of
`<name>.scenes.csv` that R8/Task 3 depends on is ambiguous in the current code)
and a handful of decision/consistency items. None are fundamental; the
architecture is sound.

**Verdict: ⚠️ PROCEED WITH UPDATES.**

---

## Product Lens Findings

### 1a. Problem Validation

- The problem is clear and concrete: an expensive coupled `agy` run does vision
  + summarize + publish together, with no way to cheapen one piece. This is a
  real operator pain, not a solution looking for a problem.
- **P1 (💡):** The ROI premise ("slide vision is the dominant cost") is stated
  as a premise to be *measured during implementation* (Motivation + Task 7),
  not established up front. That is acceptable, but the whole feature's value
  hinges on it. If the baseline in Task 7 shows vision is *not* dominant, the
  cost win evaporates while the added complexity (new stage, new backends, new
  Notion path) remains. Suggestion: capture even a rough baseline (one recording,
  wall-clock split of the current `agy` run) *before* Task 2, and add an explicit
  "if baseline shows <X% savings, stop / re-scope" checkpoint.

### 1b. User Value Assessment

- Every requirement maps to a tangible operator outcome (cheaper runs, unchanged
  behavior on rollback). Good.
- **P2 (💡):** MVP analysis — the primary value-carrying combination is
  (slides=openrouter, summarize=agy). The (openrouter, openrouter) and
  (agy, openrouter) combinations carry most of the complexity cost (they force
  the D1 Notion question) for unclear incremental value. Suggestion: consider
  shipping the primary combination first and gating the openrouter *summarize*
  backend (and thus D1) behind a follow-up, so the first cut delivers ~80% of the
  value with markedly less risk. If the author wants all four in one cut, keep
  as-is but acknowledge the tradeoff explicitly.

### 1c. Alternative Approaches

- **P3 (🤔):** Have simpler alternatives been weighed against the per-stage
  backend abstraction? E.g. a single global "cheap mode" toggle, or simply
  passing a cheaper model id to `agy` for the slide sub-task. The spec asserts
  per-stage backends are needed because the workloads differ, which is
  reasonable, but the alternatives aren't recorded. Suggestion: add a one-line
  "alternatives considered / rejected" note so future readers understand why the
  backend Protocol layer exists.

### 1d. Edge Cases & UX

- Edge cases are handled well: empty image set → empty `<name>.slides.md`
  (R9); absent/whitespace slides file treated as slides-off (R3); no silent
  fallback (R19). This is a strong point.
- **P4 (💡):** Operator discoverability of the D1-b path. If the first cut
  chooses "skip Notion + warn" for openrouter summarize, an operator who sets
  `summary.backend=openrouter` will silently lose Notion pages and only see a
  log warning. Suggestion: make this loud — surface it in `--dry-run` output and
  in `transcriber check` (e.g. "Notion publishing DISABLED: summary.backend is
  openrouter") so it's visible before a run, not just in logs after.

### 1e. Success Measurement

- **P5 (💡):** Success criteria are qualitative ("drops materially", "no visible
  quality regression", manual parity check). That's fine for a local tool, but
  "materially" is unmeasurable. Suggestion: pick a concrete target (e.g. "≥40%
  wall-time reduction on the baseline recording") so Task 7 has a pass/fail bar
  rather than a judgment call.

---

## Engineering Lens Findings

### 2a. Architecture Soundness

- The backend-Protocol split is clean, testable per layer, and keeps paid calls
  out of `pipeline.process_recording` (network-free pipeline is explicitly
  required and testable — good, Task 3 asserts it).
- Manifest-gating the new `describe_slides` stage to avoid re-issuing paid calls
  on retry is exactly right and consistent with the existing idempotency model.
- **E1 (🎯):** **`state.py` stage insertion vs. the existing `notion` stage.**
  The current `STAGES` tuple is `(pipeline, summarize, notion, telegram, s3,
  cleanup)` and `_process_one` marks *both* `summarize` and `notion` complete
  together. The spec (Task 5) says "insert `describe_slides` after `pipeline`"
  but does not address what happens to the separate `notion` stage under the
  openrouter summarize backend, where Notion is either engine-published (D1-a) or
  skipped (D1-b). Suggestion: state explicitly how `notion` completion is marked
  in each D1 branch (e.g. D1-a marks `notion` after the engine publish; D1-b
  marks `notion` complete-with-skip so retries don't loop). Also note that
  inserting a new stage changes the `STAGES` tuple — any test asserting the exact
  tuple (mentioned in Task 5) and the manifest schema for in-flight recordings
  must be updated. Old manifests won't have `describe_slides`; confirm they're
  treated as incomplete (they will be, given `_completed.get(stage, False)`), so
  a re-run re-issues the slides call once — acceptable but worth stating.

### 2b. Failure Mode Analysis

- Retry with bounded backoff on `429`/`5xx` (R15) and no silent fallback (R19)
  are specified. Good.
- **E2 (💡):** Partial-failure semantics of the openrouter *summarize* backend.
  The engine writes `<name>.md` then `<name>.telegram.md` then publishes Notion.
  If Notion (D1-a) fails after the files are written, what is the manifest state?
  The `agy` path handles this via a single agent run; the engine path has three
  sub-steps. Suggestion: specify ordering + idempotency (write both files first,
  mark `summarize` complete; publish Notion as a separately-gated step so a
  Notion failure doesn't force re-generation of the summary — mirrors the
  existing summarize/notion split intent).
- **E3 (💡):** OpenRouter response could be truncated (finish_reason=length) yet
  return HTTP 200 with partial content. R9/Task 2 treat only transport/format
  failures as errors and empty/whitespace as `""`. A truncated-but-nonempty
  summary would be silently accepted. Suggestion: check `finish_reason` and
  either retry or raise on `length` truncation for the summarize backend
  (slides is more tolerant, but worth noting).

### 2c. Security & Privacy

- Secrets-by-env-var-name (R12), no key/headers/base64 in logs (R16/R17), and a
  documented egress note (R16) are all present and consistent with the existing
  `resolve_env` pattern. Strong.
- **E4 (💡):** The vision backend base64-inlines slide images into the request.
  Slides frequently contain the most sensitive material (internal architecture,
  financials). The egress note is good; consider also noting image **retention**
  by OpenRouter/upstream providers is outside the engine's control, so operators
  in regulated contexts should keep both stages on `agy`. One sentence in the
  README privacy note.

### 2d. Performance & Scalability

- Single vision request with per-slide parts bounds cost/latency and preserves
  cross-slide dedup — a sound call. The spec acknowledges a practical ceiling of
  "tens of slides".
- **E5 (💡):** No explicit guard for the "implausibly large payload" case beyond
  a prose mention of raising `SlideDescribeError`. Suggestion: define a concrete
  bound (max slides and/or max total base64 bytes) as a module constant with a
  clear error, so the failure is deterministic rather than a provider-side 413.

### 2e. Testing Strategy

- Test requirements are specific and per-task (mock `httpx`, assert request
  shape, retry, log hygiene, manifest gating, four-combination selection). This
  is above-average rigor for a spec.
- **E6 (💡):** No test named for the openrouter-summarize partial-failure /
  Notion-failure path (ties to E2). Add one once E2 is resolved.

### 2f. Operational Readiness

- Per-call INFO observability (R17) and rollback-by-config are covered.
- Covered adequately for a local-first CLI; no additional must-address items.

### 2g. Dependencies & Integration Risks

- `httpx` already declared; no new hard dependency. Confirmed the codebase uses
  `duct` for pipeline subprocesses and `agy_headless_bridge` for the agent — the
  new backends correctly sit beside these rather than inside `pipeline`.
- **E7 (🎯):** **`<name>.scenes.csv` location is ambiguous and R8/Task 3 depend
  on it.** In `pipeline._extract_slides`, the CSV is passed to scenedetect as a
  bare filename (`scenes_csv = f"{name}.scenes.csv"`) via `list-scenes -f`,
  while `-o` is set to the slides dir. Where scenedetect actually writes that
  file (CWD vs. `-o` dir vs. alongside input) is version-dependent and *not*
  guaranteed to be the recording directory. Yet `cleanup.intermediate_paths`
  looks for it at `directory / f"{name}.scenes.csv"` (recording dir), and the
  spec's R8/Task 3 assume the backend can reliably read `<name>.scenes.csv` from
  the recording dir to build per-slide timestamps. If the file lands elsewhere,
  slide timing silently degrades to `unknown` for every slide (R8 says
  `unknown` is allowed, so it won't crash — but the whole per-slide timestamp
  feature quietly no-ops). Suggestion: (a) confirm empirically where scenedetect
  writes the CSV under the current invocation, and (b) if it's not the recording
  dir, either pass an absolute `-f` path or have `build_slide_inputs` search the
  known location. Make the resolved path explicit in Task 3 rather than assuming
  a sibling of the `.mp4`.

---

## Cross-Lens Insights

- **X1 (💡 — Scope × Risk) — RESOLVED (2026-09-24): ship all four combinations
  in one cut.** The openrouter *summarize* backend is where nearly all the
  residual risk concentrates (Notion via D1, file-write ownership, truncation
  handling, multi-step partial failure — E2/E3/E1) while *not* being the primary
  value carrier (P2). Deferring it was offered as the risk-minimizing option;
  the author has instead elected to ship all four backend combinations together.
  This is now a **consciously-accepted tradeoff**, not an oversight. The
  consequence: the must-address item **E1** (new stage × existing `notion`
  stage × manifest) and the recommendations **E2** (multi-step partial-failure
  ordering), **E3** (truncation handling), and **E6** (partial-failure test) are
  **no longer optional-by-deferral** — because the openrouter summarize path
  ships now, these become in-scope work for this cut. Severity of X1 itself
  downgrades from 🎯 to 💡 (the strategic fork is decided); its risk does not
  disappear but is redistributed onto E1/E2/E3/E6, which must be handled inline.

- **X2 (💡 — Observability × UX):** The D1-b "skip Notion + warn" path (P4) and
  the manifest handling of the `notion` stage (E1) converge: making the skip
  visible in `--dry-run`/`check` also forces a clear answer to how `notion`
  completion is recorded. Resolve them together.

---

## Findings Summary Table

| ID | Lens | Severity | Category | Finding | Suggestion |
|----|------|----------|----------|---------|------------|
| P1 | Product | 💡 | Problem Validation | ROI premise (vision = dominant cost) is unmeasured; whole feature value depends on it | Capture rough baseline before Task 2; add a "stop/re-scope if savings < X%" checkpoint |
| P2 | Product | 💡 | User Value / MVP | 3 of 4 backend combos carry most complexity for unclear incremental value | Ship (openrouter, agy) first; gate openrouter-summarize behind a follow-up |
| P3 | Product | 🤔 | Alternatives | Simpler alternatives (global toggle, cheaper model id to agy) not recorded | Add a one-line "alternatives considered/rejected" note |
| P4 | Product | 💡 | Edge Cases / UX | D1-b silently disables Notion; only a log warning | Surface in `--dry-run` and `check` output before a run |
| P5 | Product | 💡 | Success Measurement | "materially"/"no visible regression" are unmeasurable | Set a concrete target (e.g. ≥40% wall-time reduction on baseline) |
| E1 | Engineering | 🎯 | Architecture / State | Interaction of new stage with existing separate `notion` stage under each D1 branch is unspecified; STAGES tuple + manifest schema change unaddressed | State how `notion` completion is marked per D1; update tuple-asserting tests; confirm old manifests treated as incomplete |
| E2 | Engineering | 💡 | Failure Modes | Openrouter-summarize is multi-step (2 files + Notion); partial-failure/manifest semantics undefined | Write both files → mark `summarize`; publish Notion as separately-gated step |
| E3 | Engineering | 💡 | Failure Modes | Truncated (finish_reason=length) 200 response silently accepted as summary | Check `finish_reason`; retry/raise on truncation for summarize backend |
| E4 | Engineering | 💡 | Security/Privacy | Slide images (often most sensitive) base64-egressed; upstream retention out of control | Add retention caveat to README privacy note |
| E5 | Engineering | 💡 | Performance | "implausibly large payload" guard is prose-only | Define concrete max-slides / max-bytes constant with deterministic error |
| E6 | Engineering | 💡 | Testing | No test for openrouter-summarize partial/Notion-failure path | Add once E2 resolved |
| E7 | Engineering | 🎯 | Dependencies/Integration | `<name>.scenes.csv` write location is version-dependent/ambiguous; R8/Task 3 assume recording-dir sibling; timing silently degrades to `unknown` if elsewhere | Confirm scenedetect output path empirically; use absolute `-f` path or resolve in `build_slide_inputs`; make the path explicit in Task 3 |
| X1 | Both | 💡 (was 🎯) | Scope × Risk | RESOLVED: author ships all four combinations in one cut | Accepted tradeoff; risk redistributed onto E1/E2/E3/E6, which become in-scope for this cut |
| X2 | Both | 💡 | Observability × UX | D1-b visibility (P4) and `notion` manifest handling (E1) are coupled | Resolve together |

---

## Verdict

⚠️ **PROCEED WITH UPDATES.** The architecture is sound and the spec is
implementable. **X1 is resolved: the author ships all four backend combinations
in one cut** (accepted tradeoff). Because the openrouter summarize path now
ships, the two remaining must-address items — **E7** (scenes.csv location, or
the per-slide timestamp feature silently no-ops) and **E1** (how the new stage
interacts with the existing `notion` stage and the manifest under each D1
branch) — must be resolved before starting Task 2/3, and the previously-optional
recommendations **E2** (multi-step partial-failure ordering) and **E3**
(truncation handling) are now in-scope for this cut rather than deferrable. The
other items remain recommendations that improve robustness and operator clarity
but do not block progress.

---

## Remediation

Proposed spec edits for the must-address items:

1. **E7 — pin down the scenes.csv path.** In Task 3, replace "parsed from
   `<name>.scenes.csv`" with an explicit resolution step: pass scenedetect an
   absolute `-f` path (recording dir) in `pipeline._extract_slides`, and have
   `build_slide_inputs` read from that exact path; document that missing/absent
   CSV yields `unknown` per-slide (already R8) but that this is a degraded, not
   normal, path.

2. **E1 — specify `notion` stage handling.** In Task 5/Task 6, add: under
   D1-a the engine marks `notion` complete only after a successful engine-side
   publish; under D1-b the engine marks `notion` complete-with-skip and logs the
   disabled-publish warning. Add a note that inserting `describe_slides` changes
   the `STAGES` tuple and that existing manifests (lacking the key) are treated
   as incomplete, re-issuing the slides call exactly once on the next run.

3. **X1 — RESOLVED: ship all four combinations.** Record in the spec (fold into
   "Defaults / Decisions" or add "Open Decision D3 — resolved") that all four
   backend combinations ship in one cut as a consciously-accepted tradeoff.
   Consequence to note in the spec: the openrouter summarize path is in-scope
   now, so E1 (notion-stage/manifest handling), E2 (write-both-files → mark
   `summarize`, then Notion as a separately-gated step), and E3 (truncation
   check) are required inline for this cut and should be reflected in Task 5/6
   plus their test requirements (E6).

Would you like me to apply these changes to `docs/specs/slide-description-openrouter.md`? (all / select / none)
