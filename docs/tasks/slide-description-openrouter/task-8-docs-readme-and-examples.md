# Task 8 — Docs: README + examples

**Status:** [ ]

**Spec:** `docs/specs/slide-description-openrouter.md`
**Dependencies:** Task 1 (config shape is authoritative for the schema table).
Best done last so docs match the shipped surface.

## Target
`README.md`, `examples/example.config.yaml`, `examples/acme.config.yaml`, and
`tests/test_examples.py`.

## Change
- **README pipeline section:** describe the two post-transcript stages
  (`describe_slides`, `summarize`) and the per-stage `backend` selector (slides:
  `agy`|`openrouter`; summarize: `agy`|`agno`), the valid combinations, and the
  "both `agy` = today" default.
- **README schema table** (`| Field | Type | Notes |`): update/add rows for the
  nested `stages.slides` (`{enabled, backend}` — **call out the no-legacy-bool
  break**), `summary.backend`, the new `openrouter` section (`api_key_env`,
  `base_url`, `slides_model`, `summary_model`), `notion.token_env`, and
  `timeouts.slides` / `timeouts.summarize` (with the `timeouts.agy` fallback).
- **README prerequisites:** OpenRouter API key (when slides=openrouter or
  summary=agno); the `agno` optional dependency (install the `agno` extra) and its
  Node/`npx` runtime for the Notion MCP (Windows launch caveat); the Notion
  integration token env var (when summary=agno).
- **README privacy note (R16/R25/E4/E5):** non-`agy` OpenRouter paths egress
  content to a third party; slide imagery is uploaded and upstream retention is
  outside the engine's control; downscaling is a size/cost control, **not**
  redaction. Keep both stages on `agy` for sensitive content.
- **README:** note the R24 per-stage backend visibility in `--dry-run`/`check`.
- `examples/example.config.yaml`: both stages `agy`; nested
  `stages.slides: {enabled: true, backend: agy}`; no `openrouter`/`notion.token_env`.
- `examples/acme.config.yaml`: slides `openrouter` + summarize `agno`; an
  `openrouter` block (`api_key_env`, `slides_model`, `summary_model`) and
  `notion.token_env` — **env-var names only**, no secrets.

## Constraints
- No secrets in examples — env-var names only.
- Host repo configs are **out of scope**; add a follow-up note for host
  maintainers (adopt the nested `stages.slides` shape; install the `agno` extra +
  set `notion.token_env` when choosing `summary.backend=agno`).

## Ownership
Owns: `README.md`, `examples/*.yaml`, and `tests/test_examples.py`.

## Observable Acceptance
- **Tests (pytest):** both example configs load and validate; the nested
  `stages.slides` shape parses; backends parse; `openrouter` + `notion.token_env`
  present in acme, absent in example.
- **Demo:** `uv run transcriber --config examples/acme.config.yaml --dry-run`
  prints `scene-extract, transcribe, describe-slides [openrouter],
  summarize+notion [agno]`; the README schema table lists every new field.
