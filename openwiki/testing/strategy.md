---
type: guide
title: Testing — Strategy and Focused Suites
description: How pytest suites map to pipeline, orchestrator, backends, publishing, and config systems, which seams are mocked, and minimal proving-test patterns.
tags: [transcriber, testing, pytest, mocking, idempotency, config, backends]
verified:
  - by: openwiki/0.9.0
    at: 2026-11-07T16:24:10.535Z
sources:
  - id: openwiki-source-763f5d4170665c7c2eddf81e
    resource: repo://tests/test_agent.py
  - id: openwiki-source-48a50cbe3f435b2ed89ef0f8
    resource: repo://tests/test_backends.py
  - id: openwiki-source-365077cbc5ec8499bef7fc8b
    resource: repo://tests/test_cleanup.py
  - id: openwiki-source-0b904c601828cc3c83f52ea0
    resource: repo://tests/test_config_resolve.py
  - id: openwiki-source-81af13fa7982f0b3becf1286
    resource: repo://tests/test_config.py
  - id: openwiki-source-d161573f1265eef96634c931
    resource: repo://tests/test_examples.py
  - id: openwiki-source-8b176c94b018259ee14f35b7
    resource: repo://tests/test_main.py
  - id: openwiki-source-10f61ded5ac30d2609454c31
    resource: repo://tests/test_packaging.py
  - id: openwiki-source-5df3e5ec1d3c549ddc9a97c9
    resource: repo://tests/test_pipeline.py
  - id: openwiki-source-85141969915277909135a8af
    resource: repo://tests/test_routing.py
  - id: openwiki-source-9c261ec1e31471dbb9d1bcfb
    resource: repo://tests/test_s3.py
  - id: openwiki-source-e345183389ba793728e09079
    resource: repo://tests/test_slides.py
  - id: openwiki-source-5b06c848497736f00e25c944
    resource: repo://tests/test_summarize.py
  - id: openwiki-source-dea2e4c42431c82993df8686
    resource: repo://tests/test_telegram.py
  - id: openwiki-source-1aa2684f203fe19a2bf86375
    resource: repo://tests/test_transcribe.py
  - id: openwiki-source-df4110b2c5338913ae9eedcf
    resource: repo://transcriber/__main__.py
  - id: openwiki-source-bb626cf9b19692a8c8d8d744
    resource: repo://transcriber/agent.py
  - id: openwiki-source-9358a8011a5f64a66c35ac0b
    resource: repo://transcriber/cleanup.py
  - id: openwiki-source-c4777b8db8d4806695ac8b6a
    resource: repo://transcriber/config.py
  - id: openwiki-source-c09b28db65820f5184d0fc9f
    resource: repo://transcriber/pipeline.py
generated: { by: "openwiki/0.9.0", at: "2026-11-07T16:24:10.535Z" }
---

# Testing — Strategy and Focused Suites

Testing is intentionally narrow and opinionated: **no real binaries, network calls, agents, or publishing destinations are exercised in tests**. Every external is replaced with a stub at a well-defined seam, so tests stay focused on wiring, ordering, idempotency, failure isolation, config validation, log hygiene, and the absence of secrets in outputs.

The authoritative source for orchestrator behavior is [tests/test_main.py](../../tests/test_main.py). The other suites own their subsystems; this page maps each suite to its system and seam.

For the production-side explanation of why these seams exist, see [OSS Companion Transcriber Engine](../architecture/oss-companion-transcriber.md), [Pre-flight checks and idempotent retries](../operations/pre-flight-and-idempotency.md), [Transcription Pipeline](../workflows/transcription-pipeline.md), [Backend Selection](../concepts/backend-selection.md), and [Configuration model](../concepts/config-model.md).

## Suite-to-system map

| Suite | System under test | What it proves |
|---|---|---|
| [test_main.py](../../tests/test_main.py) | Orchestrator + CLI (`transcriber/__main__.py`) | Pre-flight, dry-run plan, happy-path ordering, failure isolation, manifest idempotency, disabled toggles, discovery, dry-run/disambiguation tokens |
| [test_pipeline.py](../../tests/test_pipeline.py) | Deterministic media pipeline (`transcriber/pipeline.py`) | ffmpeg/scenedetect argv, timeout threading, per-artifact skip, chunk/retry idempotency, `duct` expression |
| [test_transcribe.py](../../tests/test_transcribe.py) | OpenRouter STT backend (`transcriber/backends/transcribe_openrouter.py`) | Chunk/stitch/reconcile, diarization format, `httpx.post` seam, resume manifest |
| [test_slides.py](../../tests/test_slides.py), [test_backends.py](../../tests/test_backends.py) | Slides vision backend + shared OpenRouter vision helper | Slide-input building, downscale seam, vision call, cost/ceiling guards, retry |
| [test_agent.py](../../tests/test_agent.py) | Prompt builders (`transcriber/agent.py`) | Language/sections directives, slide-markdown (not image-path) embedding, fence growth, marker stripping |
| [test_summarize.py](../../tests/test_summarize.py) | Summarize backend selection + `agno` backend | Two-call OpenRouter flow, Notion publish seam, write-before-publish, publish-record idempotency, timeouts, log hygiene |
| [test_routing.py](../../tests/test_routing.py), [test_telegram.py](../../tests/test_telegram.py) | Telegram publish + topic routing | Route prompt, JSON partition parsing, per-topic digests, chunking, `httpx.MockTransport` payloads |
| [test_s3.py](../../tests/test_s3.py) | S3 publish (`transcriber/publish/s3.py`) | `duct.cmd` argv, `is_enabled` gating, timeout, failure surfacing |
| [test_cleanup.py](../../tests/test_cleanup.py) | Intermediate cleanup (`transcriber/cleanup.py`) | Durable vs. intermediate sets, routed digests, `transcribe_work` dir, `keep_intermediates`, sync-before-delete contract |
| [test_config.py](../../tests/test_config.py) | Config loader (`transcriber/config.py`) | JSON/YAML parity, nested `stages.slides`, mandatory `openrouter`, backend allow-lists, `resolve_env` |
| [test_config_resolve.py](../../tests/test_config_resolve.py) | Config path resolution (`resolve_config_path`) | Precedence, blank-env, shadowing, directory rejection, `using config:` line |
| [test_examples.py](../../tests/test_examples.py) | Shipped example configs (`examples/`) | Schema-drift guard: examples still load and carry OpenRouter STT fields |
| [test_packaging.py](../../tests/test_packaging.py) | Packaged prompt templates | `slide_extractor.md` + `summary.md` present and non-empty |
| [test_notion_publish.py](../../tests/test_notion_publish.py), [test_notion_mcp.py](../../tests/test_notion_mcp.py) | Notion publish paths | REST/MCP publish seams without network |

## Isolation strategy: mock at the seam, never at the edge

### Child processes: `_run_command` and `duct.cmd`

The pipeline's sole child-process seam is `_run_command(argv, timeout, stdout_path)` in `transcriber/pipeline.py`. [test_pipeline.py](../../tests/test_pipeline.py) monkeypatches it with a `Recorder` that records `(argv, timeout, stdout_path)`, materializes fake `part_*.mp3` files so downstream reads succeed, and asserts exact argv vectors (for example `ffmpeg -hide_banner -loglevel error -i … -vn -c:a libmp3lame` and the `scenedetect … detect-content … save-images` line) plus per-binary timeout threading.

The `duct` expression itself is tested separately with fake handles: `duct.cmd` is monkeypatched to capture the argv tuple and return a `FakeExpression`/`FakeHandle` whose `poll()`/`kill()`/`wait()` simulate instant completion, `stdout_path` forwarding, and the timeout-kill path (`PipelineTimeoutError` with `timeout` and `argv`). No real process is ever spawned.

### Network: `httpx.post`, `_openrouter_transcribe`, `_summarize_openrouter`

- Transcribe: [test_pipeline.py](../../tests/test_pipeline.py) stubs `_openrouter_transcribe` with a `SeamRecorder` returning canned diarized `TranscriptChunk`s (including a `fail_indices` mode for retry tests). [test_transcribe.py](../../tests/test_transcribe.py) goes one layer deeper and stubs `httpx.post` at the module seam to test chunk/stitch/reconcile with synthetic fixtures.
- Slides/vision: [test_slides.py](../../tests/test_slides.py) monkeypatches `_downscale_jpeg_to_bytes` (no real JPEGs) and `_openrouter_vision`; [test_backends.py](../../tests/test_backends.py) stubs `httpx.post` and patches `time.sleep` so retry tests run instantly.
- Summarize: [test_summarize.py](../../tests/test_summarize.py) stubs `_summarize_openrouter(config, prompt, timeout)` with a `FakeOpenRouter` that records each call and replays scripted summary/digest replies, and stubs engine-side `publish_to_notion` with a `_RecordingPublish` recorder. Telegram uses `httpx.MockTransport` to intercept every request and record URL + JSON payload; S3 intercepts `duct.cmd` with a fake handle.

### Orchestrator: `stage_calls` fixture and backend-selection seams

The central helper in [test_main.py](../../tests/test_main.py) is the `stage_calls` fixture. It monkeypatches the stage functions and records calls in order (`pipeline`, `describe_slides` backend run, `summarize` backend, `TelegramPublisher`, `s3.sync`, `cleanup`). The summarize stub writes the summary `.md` plus `<name>.telegram.md` so downstream stages (Telegram reading the digest, discovery treating the recording as done) behave as in a real run.

Backends are stubbed at the orchestrator seam (`_get_slides_backend` / `get_summarize_backend`) so no real `agy` / OpenRouter / `agno` internals run — the test stays on the wiring surface. `shutil.which` is stubbed for pre-flight binary checks; env vars are set with `monkeypatch.setenv`/`delenv`.

### Config, secrets, and log hygiene

Secrets are referenced by env-var **name** only and resolved at use time via `resolve_env`. Tests assert on names (`TG_TOKEN`, `OPENROUTER_API_KEY`, `NOTION_TOKEN`) and human-readable problem text, never on values; one test asserts the secret value is absent from `repr(cfg)`, and summarize/slides tests use `caplog` to assert keys, tokens, and fake image bytes never appear in logs.

## Test families

### Pre-flight, `check`, and dry-run

Pre-flight tests in [test_main.py](../../tests/test_main.py) cover missing binary (problem mentions the binary and "binary"), missing env var (names the variable and "environment variable"), `check` exit codes (`1` when missing, `0` when present), `aws` required only when S3 is enabled, Telegram token required only when Telegram is enabled, OpenRouter key required on every run, missing `openrouter` section failing fast, Notion token required iff `summary.backend == "agno"`, and the legacy `elevenlabs` binary no longer gating `check`.

Dry-run tests assert exit `0`, `Execution plan` output with each recording name plus Notion/Telegram/transcribe tokens (including `scene-extract` vs `describe-slides` disambiguation and `summarize+notion [<backend>]` pinning), zero stage calls, and zero `*.transcriber_state.json` manifests. Dry-run is intentionally **not** pre-flight gated: it returns `0` even when binaries/env vars are missing.

Config-path resolution in [test_config_resolve.py](../../tests/test_config_resolve.py) covers explicit `--config` beating `TRANSCRIBER_CONFIG` beating CWD candidates, blank env treated as unset, candidate-shadowing notice, directory/non-file rejection with no fallback, relative-path anchoring to the injected CWD, and the `using config:` INFO line surfaced in dry-run/`check`.

### Happy path, failure isolation, manifest, and toggles

- **Happy path:** two recordings assert the exact ordered call list (`pipeline → describe_slides → summarize/agent → telegram → [s3] → cleanup` per recording); `debug: true` behaves like `--keep-intermediates` for cleanup while the rest still runs.
- **Failure isolation:** one recording fails at summarize → overall exit `1`, failed recording gets no Telegram/cleanup, the other processes fully, and the failed manifest records pipeline complete but not summarize/cleanup.
- **Manifest idempotency:** pre-marked stages are skipped on re-run (only cleanup runs); `describe_slides` skips the paid call when the manifest says complete even if the artifact is present, and an artifact-present-but-manifest-incomplete case skips the backend call while marking the stage complete. Transcribe retry re-issues only the failed segment via the `segments.json` manifest; a `segment_seconds` change invalidates and re-chunks.
- **Disabled toggles:** disabling slides/S3/Telegram removes the stages from the plan and execution; a stale `<name>.slides.md` is never fed into summarize when slides are disabled (backend receives `None`); slides gating checks `.enabled`, not dataclass truthiness.
- **Discovery:** recordings with an existing summary `.md` are excluded; `process_all` returns results sorted by filename.

### Pipeline argv and idempotency details

[test_pipeline.py](../../tests/test_pipeline.py) asserts exact ffmpeg/scenedetect argv, `stdout_path` forwarding, per-stage timeout values, whole-run no-op when all artifacts exist (`new_artifacts == []`), single-artifact skips (`mp3`, `slides`, `txt`), and `process_all` new-vs-done detection. Transcribe tests assert no `elevenlabs` invocation, speaker-attributed `.txt` output (`Speaker <ID>:` with no timestamps), and timeout threading to the STT seam.

### Slides, prompt, summarize, and routing

- **Slides backend:** slide-input building, per-stage `slides_model` + resolved key/`base_url` threading, empty-deck and over-ceiling (`max_slides`) failures before any image is sent.
- **Agent prompt ([test_agent.py](../../tests/test_agent.py)):** `language` (`en` vs `original`), `sections` driving `##` headings, `_slide_block` embedding slide markdown verbatim in a grown code fence (never image paths), a do-not-reproduce/reference-only instruction, and `append_slide_descriptions` deterministically appending (and cleaning `[[SLIDE_EMPTY]]`/fallback placeholders from) the `## Slide Descriptions` section exactly once.
- **Summarize ([test_summarize.py](../../tests/test_summarize.py)):** selection is a pure function of config with no silent fallback (`unknown summary.backend` raises); the backend makes two OpenRouter calls (summary + digest) with the same config carrying `summary_model`/key/`base_url`; slides markdown (when present) is inlined in the first prompt; the split model keeps the `.md` slide-free while cleaned slides go to `<name>.slides-clean.md` and the Notion subpage; local files are written before publishing; the `notion_published.json` record prevents duplicate pages; empty summary fails before publish; empty digest clears a stale digest file; `_summarize_timeout` falls back to the default when unset.
- **Routing ([test_routing.py](../../tests/test_routing.py)):** the partition prompt lists every topic plus the reserved default key and demands strict JSON; `_strip_json_fence`/`_parse_route_json` tolerate fences, missing/extra keys, and non-string values; the orchestrator routes each non-empty bucket to its chat, writes per-topic digests, and falls back on partition failure.

### Cleanup contract

[test_cleanup.py](../../tests/test_cleanup.py) uses real temporary files to prove cleanup deletes exactly the intermediate set and keeps durable outputs: keeps `.mp4`, `.md`, `.txt` transcript, and `.slides-clean.md`; deletes `.mp3`, `.scenes.csv` (inside `extracted_slides.<name>/`), `.telegram.md` plus routed `<name>.telegram.<topic>.md`, `.slides.md`, `.notion_published.json`, `extracted_slides.<name>/`, and `transcribe_work.<name>/` (parts + `segments.json`). `keep_intermediates` disables all deletion; cleanup is isolated to the one recording; sync-before-delete ordering is an orchestrator caller contract (cleanup performs no upload).

### Config, schema-drift, packaging, and example guards

[test_config.py](../../tests/test_config.py) proves JSON/YAML parity, required-field errors naming the dotted path, `summary.language` (`en`/`original`), backend allow-lists (`slides.backend` in `("openrouter",)`, `summary.backend` in `("agno",)`), unconditional `openrouter` requirement, `notion.token_env` required iff backend is `agno`, `debug` boolean, timeout defaults/fill, `telegram.enabled` default `True`, `transcribe` defaults (`microsoft/mai-transcribe-2`, `diarize True`, `480`/`5`), legacy `elevenlabs` timeout silently ignored, and legacy bare-bool `stages.slides` rejected.

[test_examples.py](../../tests/test_examples.py) guards schema drift by loading the shipped `example.config.yaml`/`acme.config.yaml` and asserting OpenRouter STT fields parse; [test_packaging.py](../../tests/test_packaging.py) fails fast if `slide_extractor.md`/`summary.md` under `transcriber/prompt_templates/` are missing or empty (they ship via `packages = ["transcriber"]`).

## Minimal proving-test guidance

Keep new tests quiet and hermetic — prefer `tmp_path`, `monkeypatch`, and `caplog` over fixtures that touch disk outside the test or emit network/process noise:

- **New orchestrator stage:** extend the `stage_calls`-style fixture in [test_main.py](../../tests/test_main.py); assert the new token appears in `--dry-run`, the call appears in the happy-path order list, and its disabled toggle removes it from both plan and execution.
- **New binary/CLI call:** add a `Recorder`-style `_run_command` test in [test_pipeline.py](../../tests/test_pipeline.py) asserting exact `argv` + `timeout` + `stdout_path`, plus a skipped-when-artifact-exists case. If testing `_run_command` itself, mock `duct.cmd` with a fake handle and `time.monotonic`/`sleep`.
- **New HTTP call:** mock at `httpx.post` (or the `_summarize_openrouter`/`_openrouter_vision`/`_openrouter_transcribe` helper seam), assert the per-stage model + resolved key/`base_url` reach the call, and add a `caplog` log-hygiene test proving secrets and image bytes are absent.
- **New config field:** add a loader test in [test_config.py](../../tests/test_config.py) (parity, default, rejection message naming the dotted path) and, if it gates a stage, a pre-flight test in [test_main.py](../../tests/test_main.py) asserting the problem text names the binary or env-var name.
- **New artifact:** add a cleanup test in [test_cleanup.py](../../tests/test_cleanup.py) placing the file in `_make_all_artifacts`, asserting it is in `intermediate_paths` (or explicitly kept), and asserting `keep_intermediates` preserves it.
