# Task 8 — Submodule packaging, example configs, docs

**Status:** [ ]

**Spec:** `docs/specs/transcriber-engine.md`
**Dependencies:** Task 1 (config loader, for validating example configs). Docs can be drafted in parallel; config-load test needs Task 1.

## Target
`examples/example.config.yaml`, `examples/acme.config.yaml`, `README.md`, and the old seed `docs/seed/extraction-brainstorm.md`.

## Change
- **Example configs** reflecting each host's real behavior:
  - **Both:** slides on; Notion `insert: subpage` (subpage + link at top of parent).
  - **host A:** transcript parsing on; single Telegram chat; English summaries; no S3.
  - **host B:** S3 sync on (bucket + a named profile); topic-routed Telegram (per-topic chat ids + default fallback); original-language summaries.
- **README.md:** rewrite from the current one-liner into an engine overview — pipeline description, config schema (JSON + YAML), prerequisites (`ffmpeg`, `elevenlabs`, `aws`, `agy` authenticated; Python + `uv`), and git-submodule adoption steps for host repos.
- **Supersede** the old `docs/seed/extraction-brainstorm.md` (which said "pure pwsh") — replace/annotate it to point at `docs/seed/transcriber-engine.md`.

## Constraints
- Configs contain **no secrets** — only env-var names and non-secret ids.
- Keep the config shape aligned with Task 1's model.

## Ownership
Owns: `examples/*.config.yaml`, `README.md`, and the old seed file. Do not edit source modules.

## Observable Acceptance
- **Tests (pytest):** both example configs load cleanly under Task 1's loader (guards against schema drift).
- **Demo:** `uv run transcriber --config examples/acme.config.yaml --dry-run` loads and prints a plan; README documents the adoption steps.
