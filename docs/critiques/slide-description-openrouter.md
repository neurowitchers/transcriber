# Critique: Offload Slide Description to a Cheap OpenRouter Vision Model

**Spec under review:** `docs/specs/slide-description-openrouter.md`
**Date:** 2026-09-24
**Reviewer lenses:** Product (CEO/Product Lead) + Engineering (Staff Engineer)

---

## Executive Summary

This is a **well-scoped, well-grounded spec**. The problem is real and specific
(vision work on the heavy `agy` run is the expensive part), the solution is
minimal (one new deterministic stage + a text-only summary path), and the task
breakdown is precise, sequenced, and testable. Background claims were verified
against the codebase (`config.py`, `agent.py`, `pipeline.py`, `__main__.py`,
`state.py`, `cleanup.py`, `slide_extractor.md`) and are accurate.

The critique surfaces **no fundamental architecture flaws**. There are, however,
a handful of **must-address correctness gaps** — chiefly a timestamp/context gap
between the extractor rules and the new payload (R6), a manifest-ordering
inconsistency between the new tracked stage and the idempotent pipeline write,
and a Task 6 reference to a host config path that does not exist in this
workspace. Resolving these before implementation avoids predictable rework.

**Verdict:** ⚠️ **PROCEED WITH UPDATES** — must-address items are all resolvable
with spec edits; none require rethinking the architecture.

---

## Product Lens Findings

### 1a. Problem Validation
- **Strong.** The problem statement names the concrete cost driver ("vision work
  on the heavy agent is the expensive part") and the desired end-state (`agy`
  becomes text-only). This is a genuine cost/latency optimization, not a
  solution in search of a problem.
- **P1 (🤔 Question).** The spec asserts vision is "the expensive part" but cites
  no measurement. The whole ROI of this change rests on that premise. If the
  heavy agent's cost is dominated by the transcript/summary tokens rather than
  vision, the win is smaller than assumed. *Suggestion:* record a one-line
  baseline (approx. cost/latency of a slides-on run before vs. the projected
  OpenRouter call) in a "Motivation / Baseline" note, even if rough.

### 1b. User Value Assessment
- The user here is the operator/host. Value = lower cost + faster runs with
  identical output quality. The MVP is exactly what's specified: move vision to
  a cheap model, keep output format via the reused extractor rules.
- **P2 (💡 Recommendation).** The "value" is only realized if output quality is
  preserved. There is no acceptance criterion asserting *quality parity* (only
  format reuse via R6). *Suggestion:* add a lightweight acceptance step — run one
  real recording through both paths and eyeball the "Slide Descriptions" section
  for parity — as a manual demo in Task 6 (not an automated test).

### 1c. Alternative Approaches
- The single-call design (all slides + transcript in one request) is a sound
  choice and is justified in "Defaults / Decisions" (cross-slide dedup, bounded
  cost/latency). Good.
- **P3 (🤔 Question).** Alternative not discussed: keep vision in `agy` but swap
  `agy`'s *model* to a cheaper one. This may be simpler (no new module, no new
  stage, no new manifest entry) if `agy` supports model selection. *Suggestion:*
  add one sentence in "Defaults / Decisions" explaining why a dedicated stage
  beats simply cheapening `agy`'s model (likely: `agy` model isn't configurable
  per-stage, and decoupling lets vision and summary scale independently).

### 1d. Edge Cases & UX
- **P4 (💡 Recommendation).** Empty-result UX is underspecified. `slide_extractor.md`
  explicitly permits omitting the entire Slide Descriptions section when no scene
  carries information. In that case the model may legitimately return **near-empty
  or whitespace** markdown — but `slides_describe` (Task 2) treats
  empty/whitespace content as `SlideDescribeError`. This conflates "model failed"
  with "model correctly found nothing to describe," turning a valid outcome into
  a hard stage failure (and, per R5, a non-fallback failure). *Suggestion:*
  distinguish transport/format failure (raise) from a valid empty description
  (return `""` or a sentinel), and define how the summary prompt behaves when
  `<name>.slides.md` is empty (omit the block, like `slides` off).
- **P5 (🤔 Question).** What if slides are enabled but scenedetect produced zero
  `.jpg` files (short/blank video)? Spec says empty image list → `""` (good), but
  Task 3/Task 5 should state that an empty `.slides.md` is written (or none) and
  that summarize omits the block — otherwise the manifest `describe_slides` stage
  has ambiguous "done" semantics.

### 1e. Success Measurement
- **P6 (💡 Recommendation).** No measurable success criteria / rollback trigger.
  *Suggestion:* add: success = cost-per-slides-run drops materially with no
  quality regression on a sample; rollback = revert the stage and restore
  image-path threading in `agent.py` (the change is self-contained, so rollback
  is cheap — worth stating explicitly).

---

## Engineering Lens Findings

### 2a. Architecture Soundness
- **Good.** The new stage is a pure function of inputs + config
  (`slides_describe.describe_slides`), keeping orchestration out of the client —
  consistent with the existing module boundaries. Threading markdown-as-text into
  `agent.py` mirrors the existing `_transcript_fence`/`_fence_for` pattern.
- **E1 (🎯 Must-Address) — Stage/manifest vs. idempotent-write split is
  contradictory.** Task 3 places the *actual* `describe_slides` write inside
  `pipeline.process_recording` (idempotent, skips if file exists), while Task 5
  adds a **separately tracked** `describe_slides` manifest stage in `state.py`
  that merely "ensures `<name>.slides.md` exists ... guards/marks completion."
  This creates two owners of the same effect and an ordering hazard: the current
  `pipeline` manifest stage already runs `process_recording` end-to-end, so the
  OpenRouter call would execute **inside the `pipeline` stage**, before the new
  `describe_slides` manifest stage is ever consulted. A crash after the network
  call but before `mark_complete("pipeline")` re-runs the expensive call on
  retry, defeating the point of a dedicated tracked stage. *Suggestion:* pick one
  owner. Cleaner option: move the OpenRouter call **out** of
  `process_recording` and into the `describe_slides` orchestrator step in
  `__main__._process_one` (gated by `state.is_complete("describe_slides")`), so
  the manifest actually protects the paid call. Keep `pipeline` = ffmpeg +
  scenedetect + transcribe only. Update Task 3/Task 5 to reflect a single owner.
- **E2 (💡 Recommendation).** `STAGES` inserts `describe_slides` after `pipeline`
  (Task 5), but the manifest doc-comment and `test_state` expectations enumerate
  the tuple. *Suggestion:* Task 5 must also update the `state.py` module docstring
  ("Tracked stages ... pipeline, summarize, ...") and any test that asserts the
  full `STAGES` tuple, or those tests break. Call this out as an explicit edit.

### 2b. Failure Mode Analysis
- R5 (fail the stage, no silent fallback) and retry-via-manifest are the right
  policy and are stated clearly.
- **E3 (🎯 Must-Address) — R6 format-preservation gap: no timestamps/scene
  metadata reach the model.** `slide_extractor.md` requires per-slide output with
  a `**Timestamp:** MM:SS - MM:SS` line and alignment "to the transcript by
  timestamp." The current `agy` path lists slide image *paths* (which encode
  scene/timecode info via scenedetect's `save-images` naming and the
  `<name>.scenes.csv`). The new payload sends only base64 image bytes + the raw
  transcript text — **no timestamps, no scene boundaries, no per-image ordering
  cue** beyond message order. The model therefore cannot produce accurate
  `MM:SS - MM:SS` timestamps, so R6 ("output format and quality are preserved")
  is not actually satisfied by the described payload. *Suggestion:* include
  per-image scene timing in the payload — e.g. parse `<name>.scenes.csv` and
  prepend each `image_url` part with a small text part naming its start/end
  timestamp, or pass the scenes CSV as text context. Update Task 2's request-shape
  tests to assert timing metadata is present. If timestamps are being dropped
  deliberately, amend R6 and `slide_extractor.md` to remove the Timestamp line.
- **E4 (💡 Recommendation) — no retry/backoff on the paid call.** R5 makes an
  OpenRouter failure fail the stage, retryable only by re-running the whole
  batch. Transient 429/5xx (common with rate-limited vision endpoints) will fail
  the recording. *Suggestion:* add a small bounded retry with exponential backoff
  for 429/5xx inside `describe_slides` (still raising `SlideDescribeError` after
  exhaustion). Add a test for "retries on 429 then succeeds."
- **E5 (🤔 Question) — payload size / model context limits.** A long meeting can
  have many slides; base64 inflates size ~33%, and full transcript + N images in
  one request may exceed the model's context or provider request-size limits.
  Single-call is justified for dedup, but there is no stated ceiling. *Suggestion:*
  define behavior when image count/size is large (e.g. cap N, or note the model's
  limits and that oversized inputs are an accepted failure). At minimum document
  the expected max slides per recording.

### 2c. Security & Privacy Review
- **Good.** R3 keeps the pattern: only the env-var *name* is stored; resolved at
  use time via `resolve_env`. Verified `resolve_env` exists and behaves as
  described.
- **E6 (💡 Recommendation) — new data-egress trust boundary.** This introduces a
  new outbound flow: slide images + transcript text now leave the host to a
  third-party API (OpenRouter). The current pipeline is described as
  "local-first"; ElevenLabs already egresses audio, but slides+transcript to
  OpenRouter is a new surface worth naming. *Suggestion:* add a one-line privacy
  note in the spec (and README schema section) that enabling slides sends slide
  imagery and transcript text to OpenRouter, so operators with sensitive content
  make an informed choice. Ensure the API key is never logged.
- **E7 (💡 Recommendation).** Base64 image data and full payloads must not land in
  logs on error. *Suggestion:* Task 2 should assert that `SlideDescribeError`
  messages include status/body-snippet but **not** the base64 image bytes or the
  `Authorization` header.

### 2d. Performance & Scalability
- Single-call design bounds latency well. Idempotent skip-if-exists avoids
  repeat work (subject to E1's ownership fix).
- **E8 (🤔 Question).** Are images downscaled before encoding? Slide JPEGs from
  scenedetect may be full-resolution frames; sending full-res inflates cost and
  token usage on the vision model — undercutting the cost goal. *Suggestion:*
  consider a max-dimension downscale before base64, or explicitly note frames are
  already small enough. Either way, state the decision.

### 2e. Testing Strategy
- **Strong.** Each task has concrete, mock-based test requirements with clear
  assertions (request shape, header, one `image_url` per slide, error paths,
  no-HTTP-on-empty). Monkeypatch seams (`_run_command`,
  `pipeline.slides_describe.describe_slides`) are consistent with existing
  patterns.
- **E9 (💡 Recommendation).** Add the missing edge-case tests implied by P4/E3:
  (a) valid-but-empty model output handling, (b) timestamp metadata present in
  the request payload, (c) retry-on-429 (if E4 adopted).

### 2f. Operational Readiness
- **E10 (💡 Recommendation).** No observability requirement for the new paid
  stage. *Suggestion:* log (at INFO) the model id, slide count, and elapsed time
  per `describe_slides` call so cost/latency can be monitored — this also
  supports the P1/P6 measurement gap. Do **not** log payload contents (E7).
- Rollback: the change is self-contained; state this (see P6).

### 2g. Dependencies & Integration Risks
- **Good.** `httpx` is already declared — verified no new dependency is needed.
- **E11 (🎯 Must-Address) — Task 6 references a host config path that does not
  exist.** Task 6 instructs editing
  `flyvercity-ai-os/local-transcribe/config.yaml`, but no such file exists under
  the workspace parent (`glob **/local-transcribe/config.yaml` → 0 files). As
  written, the task is not executable in this repo. Per the submodule design,
  host configs live in *host* repos, not in `transcriber`. *Suggestion:* drop the
  host-config edit from Task 6 (it belongs to the host repo, out of scope for the
  engine), or convert it to a follow-up note ("host repos must add an
  `openrouter` block to their config"). Keep the in-repo edits (README, examples)
  as the verifiable deliverables.
- **E12 (🤔 Question).** OpenRouter requires (or strongly recommends)
  `HTTP-Referer` / `X-Title` headers for some models/rankings. *Suggestion:*
  confirm whether the target model works with only `Authorization`, or add these
  headers to the request spec in Task 2.

---

## Cross-Lens Insights

- **X1 (🎯) — Empty-description handling (P4 × E3/E9).** Both lenses converge:
  correctly finding "nothing to describe" is a valid product outcome *and* an
  engineering edge case currently mis-modeled as a hard failure. Fixing it
  improves UX and correctness simultaneously. Highest priority alongside E1/E3.
- **X2 (🎯) — Measurement gap enables the whole ROI (P1 × E10).** The product
  justification (vision is expensive) and the engineering observability gap are
  the same missing datum. Adding per-call cost/latency logging (E10) directly
  supplies the baseline the product case needs (P1/P6). One change satisfies both.
- **X3 (💡) — Scope trim reduces risk (P3 × E11).** Questioning whether a whole
  new stage is needed (P3) and removing the non-existent host-config edit (E11)
  both tighten scope. Even if the dedicated stage is kept (recommended for
  decoupling), dropping the host-repo edit removes an unexecutable, out-of-scope
  task.

---

## Findings Summary Table

| ID  | Lens        | Severity | Category                | Finding                                                                                          | Suggestion                                                                                             |
|-----|-------------|----------|-------------------------|--------------------------------------------------------------------------------------------------|--------------------------------------------------------------------------------------------------------|
| P1  | Product     | 🤔       | Problem Validation      | "Vision is the expensive part" asserted without a baseline measurement                           | Add a rough cost/latency baseline note (before vs. projected OpenRouter call)                          |
| P2  | Product     | 💡       | User Value              | No acceptance criterion asserting output-quality parity                                          | Add a manual parity demo (one real recording, eyeball the Slide Descriptions) to Task 6                |
| P3  | Product     | 🤔       | Alternatives            | Not discussed: simply cheapen `agy`'s vision model instead of a new stage                        | Add one sentence justifying the dedicated stage over per-stage `agy` model swap                        |
| P4  | Product     | 💡       | Edge Cases              | Valid "nothing to describe" output mis-modeled as `SlideDescribeError`                           | Distinguish transport/format failure from valid empty result; define summary behavior on empty file    |
| P5  | Product     | 🤔       | Edge Cases              | Ambiguous `describe_slides` "done" semantics when zero `.jpg` produced                           | State whether an empty `.slides.md` is written and that summarize omits the block                      |
| P6  | Product     | 💡       | Success Measurement     | No measurable success criteria or rollback trigger                                               | Define success (cost drop, no quality regression) and note the self-contained rollback                |
| E1  | Engineering | 🎯       | Architecture            | Two owners of the `<name>.slides.md` write (pipeline stage vs. manifest stage) → paid call unprotected on retry | Single owner: move the OpenRouter call into the `describe_slides` orchestrator step guarded by manifest |
| E2  | Engineering | 💡       | Architecture            | `STAGES` change breaks `state.py` docstring + tuple-asserting tests                              | Task 5 must also update the docstring and affected `test_state` assertions                             |
| E3  | Engineering | 🎯       | Failure Modes / R6      | Payload sends no timestamps/scene metadata; extractor requires `MM:SS` timestamps → R6 unmet     | Include per-image scene timing (parse `<name>.scenes.csv`) or amend R6/extractor to drop timestamps    |
| E4  | Engineering | 💡       | Failure Modes           | No retry/backoff on the paid call; transient 429/5xx fails the recording                         | Add bounded exponential backoff for 429/5xx inside `describe_slides`; add a retry test                 |
| E5  | Engineering | 🤔       | Performance             | Single call may exceed model context / request-size limits for long meetings                     | Define max slides/size behavior; document expected ceiling                                             |
| E6  | Engineering | 💡       | Security / Privacy      | New data egress (slides + transcript) to a third party; not called out                           | Add a privacy note in spec + README schema section; never log the key                                  |
| E7  | Engineering | 💡       | Security / Privacy      | Risk of base64 bytes / auth header leaking into error logs                                       | Assert error messages exclude image bytes and the `Authorization` header                               |
| E8  | Engineering | 🤔       | Performance             | Full-res JPEGs inflate cost/tokens, undercutting the savings goal                                | Downscale to a max dimension before base64, or document frames are already small                      |
| E9  | Engineering | 💡       | Testing                 | Missing tests for empty output, timestamp metadata, retry                                        | Add the three edge-case tests (ties to P4/E3/E4)                                                       |
| E10 | Engineering | 💡       | Operational Readiness   | No observability for the new paid stage                                                          | Log model id, slide count, elapsed time per call (not payload contents)                                |
| E11 | Engineering | 🎯       | Dependencies/Integration| Task 6 edits `flyvercity-ai-os/local-transcribe/config.yaml` which does not exist in workspace   | Drop the host-config edit (out of scope for the engine) or make it a follow-up note                     |
| E12 | Engineering | 🤔       | Dependencies/Integration| OpenRouter may need `HTTP-Referer`/`X-Title` headers                                              | Confirm header requirements; add to Task 2 request spec if needed                                      |
| X1  | Both        | 🎯       | Correctness × UX        | Empty-description handling (P4 × E3/E9)                                                           | Model "found nothing" as a valid empty result, not a failure                                           |
| X2  | Both        | 🎯       | Measurement × Observability | ROI premise (P1) and observability gap (E10) are the same missing datum                       | Per-call cost/latency logging supplies the baseline the product case needs                             |
| X3  | Both        | 💡       | Scope × Risk            | New-stage necessity (P3) + non-existent host-config edit (E11)                                   | Trim host-repo edit from Task 6; keep dedicated stage for decoupling                                    |

---

## Verdict

⚠️ **PROCEED WITH UPDATES**

The spec is sound and implementation-ready in structure. Resolve the four
must-address items before implementing:

1. **E1** — single owner for the paid `describe_slides` write so the manifest
   actually protects the OpenRouter call on retry.
2. **E3 / X1** — feed per-slide timestamp/scene metadata into the payload (or
   amend R6 + the extractor to drop timestamps) so R6's format-preservation claim
   holds.
3. **P4 / X1** — treat a valid empty description distinctly from a failure, and
   define the summary prompt's behavior on an empty `<name>.slides.md`.
4. **E11** — remove/relocate the non-existent host-config edit in Task 6.

The recommendations (E2, E4, E6, E7, E9, E10, P2, P6) meaningfully de-risk the
change and should be folded in; the questions (P1, P3, P5, E5, E8, E12) need a
quick author decision but do not block.

---

## Remediation Offer

I can apply targeted edits to `docs/specs/slide-description-openrouter.md` for
each must-address item and recommendation — for example:

- Rework Task 3/Task 5 so the OpenRouter call lives in the manifest-gated
  `describe_slides` orchestrator step (E1), including the `state.py` docstring/
  test note (E2).
- Extend Task 2 and R6 to carry per-slide timestamps from `<name>.scenes.csv`
  into the payload, with matching request-shape tests (E3, E9).
- Add empty-result semantics to R-list, Task 2, and Task 4 (P4/P5/X1).
- Replace the Task 6 host-config edit with a follow-up note (E11).
- Add retry/backoff (E4), a privacy note (E6), log-hygiene assertions (E7), and
  per-call observability (E10).

**Would you like me to apply these changes? (all / select / none)**

After applying, re-run `Critique` to verify the updates resolve the findings.
