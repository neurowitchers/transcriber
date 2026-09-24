# Spec: Two Pluggable Post-Transcript Stages (Slide Description + Summarize) with Selectable Backends

## Problem Statement

Today a single heavy `agy` (local CLI agent) run does three things at once:
describes the extracted slide images (vision), summarizes the transcript, and
publishes to Notion + writes the Telegram digest. This couples an expensive
vision workload to the summary/publish workload and gives the operator no way to
choose a cheaper engine for either piece independently.

We want to **cleanly split the post-transcript work into two stages** and let
each stage's **backend be configured independently**:

1. **`describe_slides`** — turn the extracted slide images (+ transcript
   context) into a `<name>.slides.md` markdown block.
2. **`summarize`** — turn the transcript (+ the `<name>.slides.md` block) into
   the final `<name>.md` summary and `<name>.telegram.md` digest, and publish to
   Notion.

Each stage can run on one of two **backends**, chosen per stage in config:

- **`agy`** — the local headless CLI agent (current behavior).
- **`openrouter`** — a direct OpenRouter API call (cheaper / faster models).

The two selections are **orthogonal**: e.g. slides on `openrouter` + summarize on
`agy` (the primary cost-saving configuration), both on `agy` (today's behavior),
both on `openrouter`, or slides on `agy` + summarize on `openrouter`.

## Motivation, Baseline & Success

- **Baseline (record during implementation).** The ROI premise is that slide
  vision is the dominant cost of a slides-on `agy` run. Capture a rough baseline
  (wall time, and token/cost if the bridge surfaces it) for one representative
  slides-on recording with both stages on `agy`. Compare against slides on
  `openrouter` + summarize on `agy`.
- **Why per-stage backends instead of one global switch.** The two workloads
  have different needs: slide description is vision-heavy and stateless
  (great fit for a cheap vision API); summarize is text-heavy and must publish
  to Notion and write local files (needs the agent's MCP + file tools, or engine
  glue if done via API). Making the backend a per-stage choice lets the operator
  optimize each independently without degrading the other.
- **Success criteria.** With slides on `openrouter` + summarize on `agy`,
  cost/latency of a slides-on run drops materially with no visible quality
  regression in the "Slide Descriptions" section (manual parity check, Task 7).
- **Rollback.** Setting both stages' backend to `agy` reproduces today's
  behavior exactly; the split is additive, so rollback is a config change.

## Requirements

### Stage separation

- **R1.** The post-transcript work is split into two explicit, independently
  tracked stages: `describe_slides` (produces `<name>.slides.md`) and `summarize`
  (produces `<name>.md` + `<name>.telegram.md` and publishes to Notion).
- **R2.** `describe_slides` runs only when `stages.slides` is enabled. When
  disabled, no `<name>.slides.md` is produced and `summarize` omits the slide
  block (unchanged behavior).
- **R3.** `summarize` consumes `<name>.slides.md` **as text**. An absent or
  empty/whitespace `<name>.slides.md` is treated exactly like slides-off (block
  omitted).

### Backend selection

- **R4.** Each stage has an independent backend selector in config:
  `slides.backend` and `summary.backend`, each one of `"agy" | "openrouter"`.
  Defaults preserve current behavior: both default to `"agy"`.
- **R5.** The four combinations are all valid and behave correctly:
  (agy, agy) = today; (openrouter, agy) = primary cost saver; (agy, openrouter);
  (openrouter, openrouter).
- **R6.** Backend selection for one stage does not change the other stage's
  behavior, artifacts, or contracts. Both backends of a stage produce the same
  output artifact(s) with the same semantics.

### `describe_slides` behavior

- **R7.** `agy` backend: `agy` is driven (as today's slide path) to read the
  slide images and write `<name>.slides.md`. `openrouter` backend: a single
  chat-completions vision call produces the markdown.
- **R8.** Both backends reuse the existing `slide_extractor.md` rules so output
  format is preserved. Because the extractor requires per-slide
  `**Timestamp:** MM:SS - MM:SS` lines, per-slide scene timing (parsed from
  `<name>.scenes.csv`) is provided to the backend: for `openrouter`, as a text
  part immediately before each image; for `agy`, alongside the image paths.
  Unknown timing is passed as `unknown`, never omitted.
- **R9.** Empty-result semantics: an empty image set or a valid empty backend
  response both resolve to an **empty `<name>.slides.md`**; the stage still
  completes successfully and is marked done. A backend *transport/format*
  failure raises `SlideDescribeError` (not triggered by an intentionally empty
  result).

### `summarize` behavior

- **R10.** `agy` backend: unchanged — `agy` writes `<name>.md` +
  `<name>.telegram.md` and publishes to Notion via its MCP (the current
  file-is-source-of-truth contract). `openrouter` backend: the engine calls
  OpenRouter to produce the summary markdown and the digest text, **the engine
  writes** `<name>.md` and `<name>.telegram.md` itself, and Notion publishing is
  handled by the engine's own Notion path (see R11).
- **R11.** **Notion publishing under the `openrouter` summarize backend.**
  OpenRouter is a plain LLM API with no MCP/file/Notion tools, so it cannot
  publish to Notion or write files. This is an explicit architectural
  constraint. The spec resolves it as follows: when `summary.backend ==
  "openrouter"`, the engine (a) writes the summary + digest files from the API
  response, and (b) publishes to Notion via a small engine-owned Notion client
  (new, minimal) OR, if that is out of scope for the first cut, **skips Notion
  and logs a clear warning** that Notion publishing requires the `agy` summarize
  backend. Task 6 makes this an explicit decision point (see "Open Decision D1").
  Telegram dissemination is unaffected (it already reads the digest file in the
  orchestrator, independent of backend).

### Cross-cutting (both backends / stages)

- **R12.** Secrets follow the existing pattern: config stores the OpenRouter
  API-key **env-var name** only; resolved at use time via `resolve_env`. No
  secrets in config.
- **R13.** `openrouter` connection config (`api_key_env`, `base_url`, and
  per-stage `model`) is configurable. `base_url` defaults to
  `https://openrouter.ai/api/v1`. The slides model defaults to a cheap vision
  model (`google/gemini-2.0-flash-001`); the summary model defaults to a cheap
  text model (`google/gemini-2.0-flash-001` is acceptable for both, but the two
  are configured separately so they can diverge). Per-stage timeouts
  `timeouts.slides` and (reuse of) `timeouts.agy` apply; add `timeouts.slides`
  (default 900s).
- **R14.** Pre-flight validation: whichever backends are selected, the required
  inputs must be present. If **either** stage uses `openrouter`, the config must
  include an `openrouter` section and its API-key env var is checked. If a stage
  uses `agy`, the `agy` binary must be on PATH (existing check). If slides use
  `openrouter` but summarize uses `agy` (and vice versa), only the actually-used
  backends' prerequisites are required.
- **R15.** Transient-failure resilience for `openrouter` calls: retry `429`/`5xx`
  with bounded exponential backoff before raising.
- **R16.** Privacy / data egress: any `openrouter` backend sends content to a
  third party — slides backend sends slide imagery + transcript context;
  summarize backend sends the transcript + slide markdown. Documented in the spec
  and README. API key, auth headers, and image bytes are never logged.
- **R17.** Observability: each `openrouter` call logs (INFO) the stage, model id,
  input size (slide count or transcript length), and elapsed time — never payload
  contents (R16).
- **R18.** Cleanup deletes `<name>.slides.md`; `--keep-intermediates` preserves
  it; it is never confused with the durable `<name>.md`.
- **R19.** Failure isolation is per recording and per stage (existing manifest
  behavior). No silent cross-backend fallback: a selected backend's failure fails
  that stage (retryable), it does not silently switch to the other backend.

## Background (Codebase Internals)

- **`transcriber/agent.py`**
  - `_slide_block(slide_image_paths)` reads `prompt_templates/slide_extractor.md`
    and lists image paths in a "Slide descriptions" instruction block; `""` when
    no slides.
  - `build_prompt(...)` threads the slide block into
    `prompt_templates/summary.md`'s `{slide_block}` placeholder; transcript is
    wrapped in a non-XML Markdown fence (`_transcript_fence` / `_fence_for`).
  - `run_agent(config, transcript_path, recording_dir, slide_image_paths)`
    resolves the output path, builds the prompt (`inline_transcript=False`),
    exposes the workspace via `add_dirs`, and drives `agy` through
    `agy_headless_bridge.run`. `agy` writes `<name>.md` + `<name>.telegram.md`
    and publishes to Notion via MCP.
- **`transcriber/pipeline.py`** — `process_recording` runs ffmpeg → scenedetect
  (optional) → transcribe (`--format text` → `.txt`). Idempotent per stage.
  `_run_command` is the single child-process seam (monkeypatched in tests).
- **`transcriber/__main__.py`** — `_process_one` drives stages via the state
  manifest: `pipeline` → `summarize`(+`notion`) → `telegram` → `s3` → `cleanup`.
  `_slide_image_paths(mp4, config)` lists `extracted_slides.<name>/*.jpg`.
  `_transcript_path` → `<name>.txt`. `_enabled_stage_names` builds the dry-run
  plan; `preflight_check` / `_required_env_vars` verify binaries + env vars.
- **`transcriber/state.py`** — `STAGES = (pipeline, summarize, notion, telegram,
  s3, cleanup)`; only listed stages tracked; `mark_complete` persists at once.
- **`transcriber/cleanup.py`** — `INTERMEDIATE_SUFFIXES = (".mp3", ".txt",
  ".scenes.csv")`; `intermediate_paths` also lists `<name>.telegram.md` + the
  slides dir; `KEEP_SUFFIXES = (".mp4", ".md")`; `_has_keep_suffix`
  special-cases `.telegram.md` so the digest is deletable.
- **`transcriber/config.py`** — dataclass model + `_build_section`; optional
  sections (`s3`) present-or-`None`; `Timeouts` fields default to 900;
  `resolve_env` resolves secrets at use time.
- **Telegram** — the orchestrator reads `<name>.telegram.md` (fallback
  `<name>.md`) and sends it; independent of the summarize backend.
- **Dependencies** — `httpx` already declared; no new dependency needed for
  OpenRouter calls. A minimal Notion HTTP client (R11/D1) would also use `httpx`.
- **OpenRouter** — OpenAI-compatible `POST {base_url}/chat/completions`. Vision:
  message `content` parts of `{"type":"text",...}` and
  `{"type":"image_url","image_url":{"url":"data:image/jpeg;base64,<b64>"}}`.
  Text: plain text parts. Header `Authorization: Bearer <key>`; response text at
  `choices[0].message.content`.

## Proposed Solution

### Stage/backend structure

Two stages, each behind a small **backend interface**, selected by config:

```
describe_slides stage  -> backend in {AgySlidesBackend, OpenRouterSlidesBackend}
summarize stage        -> backend in {AgySummarizeBackend, OpenRouterSummarizeBackend}
```

- Each stage is a manifest-tracked orchestrator step in `__main__._process_one`.
  The `pipeline` stage stays ffmpeg + scenedetect + transcribe only (no network).
- `describe_slides` (when `stages.slides`) runs after `pipeline`, before
  `summarize`, and writes `<name>.slides.md`. Manifest-gated so a paid backend
  call is not re-issued on retry.
- `summarize` reads the transcript + `<name>.slides.md` and produces `<name>.md`
  + `<name>.telegram.md` (+ Notion). Both backends yield the same artifacts.

```mermaid
flowchart TD
    P[pipeline: ffmpeg + scenedetect + transcribe] --> Q{stages.slides?}
    Q -- yes --> DS[describe_slides stage]
    Q -- no --> SUM[summarize stage]
    DS -->|backend=agy| DA[agy reads images -> slides.md]
    DS -->|backend=openrouter| DO[OpenRouter vision call -> slides.md]
    DA --> SUM
    DO --> SUM
    SUM -->|backend=agy| SA[agy writes md + digest + Notion]
    SUM -->|backend=openrouter| SO[OpenRouter text call -> engine writes md + digest; Notion per D1]
    SA --> T[telegram] --> S3[s3] --> CL[cleanup]
    SO --> T
```

### Config model additions

```python
BACKENDS = ("agy", "openrouter")

@dataclass
class SlidesStage:
    enabled: bool                 # was stages.slides
    backend: str = "agy"          # "agy" | "openrouter"

@dataclass
class Summary:                    # existing; add backend
    language: str                 # "en" | "original"
    sections: list[str]
    backend: str = "agy"          # "agy" | "openrouter"

@dataclass
class OpenRouter:
    api_key_env: str
    base_url: str = "https://openrouter.ai/api/v1"
    slides_model: str = "google/gemini-2.0-flash-001"   # vision
    summary_model: str = "google/gemini-2.0-flash-001"  # text

@dataclass
class Timeouts:
    ffmpeg: int = 900
    scenedetect: int = 900
    slides: int = 900             # NEW
    elevenlabs: int = 900
    agy: int = 900
    s3: int = 900

# Config: openrouter: Optional[OpenRouter] = None
# Validation: openrouter required iff any selected backend == "openrouter";
#             backend values validated against BACKENDS.
```

Backward-compat note: the existing `stages.slides: bool` is generalized. Keep the
loader tolerant — accept the legacy `stages.slides: true/false` bool and map it
to `SlidesStage(enabled=..., backend="agy")`, OR require the new nested shape and
update all configs/examples in Task 6. **Open Decision D2** (Task 1) picks one;
default recommendation: accept both shapes for a smooth migration.

### Backend interfaces (sketch)

```python
# transcriber/backends/slides.py
class SlidesBackend(Protocol):
    def describe(self, slides: Sequence[SlideInput], transcript_text: str,
                 config: Config, *, timeout: float) -> str: ...

# transcriber/backends/summarize.py
class SummarizeBackend(Protocol):
    def summarize(self, transcript_path: Path, slides_markdown: str | None,
                  recording_dir: Path, config: Config) -> SummaryResult: ...
    # SummaryResult: paths to <name>.md and <name>.telegram.md (both written).
```

- `AgySlidesBackend` drives `agy` to write `<name>.slides.md` (reuses the
  existing prompt-building + `run_agent` seam, scoped to the slides sub-task).
- `OpenRouterSlidesBackend` = the single vision chat-completions call
  (timestamps + downscale + retry) described below.
- `AgySummarizeBackend` = today's `run_agent` (writes md + digest + Notion).
- `OpenRouterSummarizeBackend` = text chat-completions call; the **engine** writes
  the two files and handles Notion per R11/D1.

### OpenRouter vision call (slides backend)

Behavior mirrors the prior design: resolve key via `resolve_env`; read
`slide_extractor.md`; build one user message = leading text part (rules +
language + transcript context), then per slide a `Timestamp: MM:SS - MM:SS` text
part (from `<name>.scenes.csv`; `unknown` when missing) + `image_url` part;
**downscale** each JPEG to a bounded long edge (module constant, e.g. 1024 px)
before base64; headers `Authorization`, `HTTP-Referer`, `X-Title`; POST with
`timeout`; retry `429`/`5xx` with bounded backoff; return
`choices[0].message.content` (empty/whitespace → `""`). Single call bounds
cost/latency and preserves cross-slide dedup. Practical ceiling: tens of slides;
implausibly large payload → `SlideDescribeError`.

## Task Breakdown

### Task 1 — Config: per-stage `backend`, `openrouter` section, validation

**Objective.** Generalize `stages.slides` to carry a `backend`; add
`summary.backend`; add an optional `OpenRouter` section (api_key_env, base_url,
slides_model, summary_model); add `timeouts.slides`. Validate backend values
against `("agy","openrouter")` and require `openrouter` iff any selected backend
is `openrouter`.

**Implementation guidance.** Resolve **Open Decision D2** (accept legacy
`stages.slides: bool` and coerce to `SlidesStage(enabled, backend="agy")`, vs.
require nested shape). Recommended: accept both. Follow the `S3` optional-section
pattern for `openrouter`. Put backend-value validation and the
"openrouter-required-iff-used" cross-field check next to the `summary.language`
validation.

**Test requirements** (`tests/test_config.py`):
- Defaults: omitted backends → both `"agy"`; no `openrouter` needed.
- `slides.backend: openrouter` (or `summary.backend: openrouter`) without
  `openrouter` section → `ConfigError`.
- Invalid backend value → `ConfigError` listing allowed values.
- `openrouter` parses `base_url`/`slides_model`/`summary_model` defaults.
- Legacy `stages.slides: true` still loads (if D2 = accept-both).
- `timeouts.slides` defaults to 900 and is overridable.

**Demo.** Load each of the four backend combinations; show a missing
`openrouter` section error when a stage selects it.

### Task 2 — Backend interfaces + OpenRouter client core

**Objective.** Introduce the `SlidesBackend` / `SummarizeBackend` interfaces and
a shared OpenRouter HTTP helper (auth headers, retry/backoff, log hygiene,
error type). No stage wiring yet.

**Implementation guidance.** Add `transcriber/backends/` (or top-level modules).
Shared helper `_openrouter_chat(config, messages, model, *, timeout) -> str`
handling headers, `429`/`5xx` retry, and `SlideDescribeError`/`SummarizeError`
on transport/format failure. Log hygiene: never include key/headers/base64 in
errors or logs.

**Test requirements** (`tests/test_backends.py`, mock `httpx`):
- Helper sends `Authorization`+`HTTP-Referer`+`X-Title`, correct URL, model.
- Retry `429`→`200` succeeds (patch sleep); persistent `5xx` → error after cap.
- Malformed JSON / missing content → error.
- Errors carry status + short snippet only (no secrets/bytes).

**Demo.** Drive the helper against a mocked transport for success, retry, and
failure.

### Task 3 — Slides backends (agy + openrouter) + slide-input helper

**Objective.** Implement `AgySlidesBackend` and `OpenRouterSlidesBackend`, plus
`build_slide_inputs(recording_dir, name)` (ordered images joined to
`<name>.scenes.csv` timing; missing → `unknown`; zero images → `[]`). Both
backends write/return the slide markdown; empty result → `""` (R9). Pipeline
stays network-free.

**Implementation guidance.** OpenRouter backend = the vision call above
(timestamps, downscale, single request). Agy backend = a scoped `agy` run that
writes `<name>.slides.md` only (reuse prompt building). Neither lives in
`process_recording`.

**Test requirements** (`tests/test_backends.py` / `tests/test_slides.py`):
- `build_slide_inputs`: stable order, timing joined from fixture CSV, `unknown`
  on miss, `[]` on zero images.
- OpenRouter backend request shape: one `image_url` per slide, `Timestamp:` part
  before each; empty images → no HTTP call, returns `""`.
- Agy backend (mock `run_agent`): writes `<name>.slides.md`.
- `pipeline.process_recording` makes no slides-backend call.

**Demo.** Run each backend (mocked) over a fixture slides dir → `<name>.slides.md`.

### Task 4 — Summarize backends (agy + openrouter)

**Objective.** Implement `AgySummarizeBackend` (today's `run_agent`) and
`OpenRouterSummarizeBackend` (engine calls OpenRouter for summary + digest text,
engine writes `<name>.md` + `<name>.telegram.md`; Notion per R11/D1). Both take
`slides_markdown` (text) and omit the slide block when empty (R3).

**Implementation guidance.** Refactor `agent.py` so `_slide_block` accepts
`slides_markdown: str | None` (embed fenced text when non-empty; omit when
empty/whitespace) — no image paths. `AgySummarizeBackend` keeps the
file-is-source-of-truth + Notion-via-MCP contract. `OpenRouterSummarizeBackend`
builds a text prompt from `summary.md` template + transcript + slide markdown,
calls the summary model, writes both files, then performs Notion per D1
(engine-owned minimal Notion client, or skip-with-warning for the first cut).

**Test requirements** (`tests/test_agent.py` / `tests/test_backends.py`):
- `_slide_block` embeds markdown when non-empty; omits on `None`/`""`/whitespace;
  no image paths in the prompt.
- Agy summarize: existing behavior/tests intact (writes md/digest, Notion prompt).
- OpenRouter summarize (mock httpx): writes `<name>.md` + `<name>.telegram.md`
  from the response; on `summary.backend=openrouter`, Notion is either published
  via the engine client or a clear warning is logged (per D1).

**Demo.** Produce a summary via each backend (mocked) and show both files written.

### Task 5 — Orchestrator wiring (`__main__.py` + `state.py`)

**Objective.** Add the manifest-tracked `describe_slides` stage (gated on
`stages.slides.enabled`) that selects and runs the configured slides backend and
writes `<name>.slides.md`; change the `summarize` stage to select and run the
configured summarize backend, passing the slide markdown. Feed Telegram from the
digest as today.

**Implementation guidance.**
- `state.py`: insert `"describe_slides"` after `"pipeline"`; update the docstring
  "Tracked stages" list and any `STAGES`-tuple-asserting test.
- `_process_one`: `describe_slides` step (manifest-gated) → build slide inputs,
  read transcript, run slides backend, write `<name>.slides.md` (idempotent skip
  if present), log stage/model/size/elapsed (R17), mark complete. Then
  `summarize` step selects the backend and runs it. A backend failure fails the
  recording (isolated); an empty slide result writes an empty file and completes.
- Backend selection = pure function of config (`slides.backend`,
  `summary.backend`).
- `_enabled_stage_names`: add `"describe-slides"` after `transcribe` when slides
  on; keep `"summarize"`.
- `preflight_check`/`_required_env_vars`: require the OpenRouter env var iff any
  selected backend is `openrouter`; require `agy` on PATH iff any selected
  backend is `agy`.

**Test requirements** (`tests/test_main.py`, monkeypatch backends):
- Dry-run plan lists `describe-slides` only when slides on.
- Correct backend chosen per config for each of the four combinations.
- `describe_slides` manifest-gated: skipped on retry when complete.
- Summarize receives slide markdown; no image paths.
- Pre-flight requires OpenRouter env var only when a stage uses it; requires
  `agy` only when a stage uses it.

**Demo.** `--dry-run` shows `describe-slides`; a mocked (openrouter, agy) run
writes `<name>.slides.md` then summarizes via `agy`; a re-run skips the paid call.

### Task 6 — Notion under the OpenRouter summarize backend (Decision D1)

**Objective.** Resolve how Notion publishing works when `summary.backend ==
"openrouter"` (OpenRouter cannot use MCP/tools).

**Open Decision D1 (author picks in this task):**
- **D1-a (recommended, fuller):** add a minimal engine-owned Notion client
  (`transcriber/publish/notion.py`, `httpx`) that creates the subpage under
  `notion.parent_page_id` and links it at the top of the parent — mirroring what
  the `agy` prompt instructs. Requires a Notion integration token (new env-var
  name in config, secret-by-name per R12).
- **D1-b (smaller first cut):** when `summary.backend=openrouter`, **skip Notion
  and log a clear warning** that Notion publishing requires `summary.backend=agy`.
  Files + Telegram still work.

**Implementation guidance.** Pick D1-a or D1-b and implement; if D1-a, add the
Notion token env-var to config + pre-flight (required iff openrouter summarize).

**Test requirements.** D1-a: mock the Notion client; assert subpage-create call
shape and token-by-name resolution. D1-b: assert the warning path and that files
+ Telegram still proceed.

**Demo.** Run an (any, openrouter) summarize (mocked) and show Notion published
(D1-a) or the warning logged with files written (D1-b).

### Task 7 — Cleanup, docs/config, verification

**Objective.** Cleanup for `<name>.slides.md`; docs/examples for the new config;
full verification.

**Implementation guidance.**
- `cleanup.py`: add `".slides.md"` with a `_has_keep_suffix` special-case (like
  `.telegram.md`) so it is deletable while `<name>.md` is kept; add to
  `intermediate_paths`; `--keep-intermediates` preserves it.
- `README.md`: document the two stages, the per-stage `backend` selector, the
  four combinations, the `openrouter` section + `timeouts.slides`, and a
  **privacy note** (R16) that `openrouter` backends egress content to a third
  party. Note the D1 Notion behavior.
- `examples/example.config.yaml` / `acme.config.yaml`: show the new shape
  (e.g. example = both `agy`; acme = slides `openrouter` + summarize `agy` with
  an `openrouter` block, env-var name only).
- Host config edits are **out of scope** (host repos own their configs; provide
  a follow-up note for host maintainers).

**Test requirements.**
- `tests/test_cleanup.py`: deletes `<name>.slides.md`, keeps `<name>.md`/`.mp4`;
  `--keep-intermediates` preserves it.
- `tests/test_examples.py`: example configs load; backends parse; `openrouter`
  present where used.

**Demo.** `uv run pytest -q` green. Manual parity check (R-success): one real
slides-on recording with slides `openrouter` + summarize `agy`; eyeball the
Slide Descriptions vs. a prior all-`agy` run.

## Open Decisions (resolve during implementation)

- **D1 — Notion under OpenRouter summarize backend:** engine-owned Notion client
  (D1-a) vs. skip-with-warning (D1-b). Task 6.
- **D2 — Config migration for `stages.slides`:** accept legacy bool + coerce
  (recommended) vs. require the new nested `{enabled, backend}` shape. Task 1.

## Defaults / Decisions

- **Backends default to `agy`** for both stages → today's behavior with no config
  change (R4).
- **Orthogonal selection:** the two backend choices are independent (R5/R6).
- **Single owner for paid calls:** OpenRouter calls live in the manifest-gated
  orchestrator steps, not in `pipeline.process_recording`, so retries don't
  re-issue paid calls.
- **Slides OpenRouter call:** single request, per-slide timestamps from
  `scenes.csv`, JPEG downscale to a bounded long edge, `429`/`5xx` backoff.
- **No silent fallback (R19):** a selected backend's failure fails the stage
  (retryable); it never silently switches backends. A *valid empty* slide result
  is not a failure.
- **Notion constraint (R11):** OpenRouter can't publish to Notion; the engine
  either publishes via its own client (D1-a) or skips with a warning (D1-b).
- **Privacy (R16) / Observability (R17):** documented egress; per-call INFO logs
  of stage/model/size/elapsed; secrets and bytes never logged.
- **Dependencies:** `httpx` already declared; the optional Notion client (D1-a)
  reuses it. No new hard dependency.
