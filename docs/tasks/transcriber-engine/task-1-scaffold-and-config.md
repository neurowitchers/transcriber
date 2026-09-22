# Task 1 — Project scaffold + config loader (JSON/YAML)

**Status:** [ ]

**Spec:** `docs/specs/transcriber-engine.md`
**Dependencies:** none (foundation task — other tasks import this)

## Target
`pyproject.toml`, the `transcriber/` package root, and `transcriber/config.py`.

## Change
- Create a `uv`-managed Python project: `pyproject.toml` with a `transcriber` console entry point.
- Dependencies: `duct`, `agy-headless-bridge`, `scenedetect`, `av`, `PyYAML`, `httpx`. Dev: `pytest`.
- Implement `transcriber/config.py` that loads `transcriber.config.json` / `.yaml` / `.yml` by file extension into a single validated dataclass model. Both formats map to the identical model.
- Secrets are referenced by env-var **name** in config and resolved from the environment only at use time (never stored in the config or the model as literal values).

### Config model (authoritative)
```
Config
  recordings_dir: str
  stages: { slides: bool, parse_transcript: bool, s3_sync: bool }
  transcribe: { model_id: str }
  summary: { language: "en"|"original", sections: list[str] }
  agent: { cli: str, extra_args: list[str], output_file: str }   # output_file templated with {basename}
  notion: { server: str, parent_page_id: str, insert: "subpage" }
  telegram: { bot_token_env: str, default_chat_id: str, routing: dict[str,str] }
  s3: { bucket: str, profile: str } | None
  timeouts: { ffmpeg: int, scenedetect: int, elevenlabs: int, agy: int, s3: int }   # seconds
```

## Constraints
- Provide sensible defaults for `timeouts` when omitted.
- A missing **required** field raises a clear, actionable error naming the field.
- A referenced env var that is absent raises a readable error naming the var (raised at use time, not necessarily at load time).
- Do not implement any pipeline/publishing logic here — this is scaffold + config only.

## Ownership
Owns: `pyproject.toml`, `transcriber/__init__.py`, `transcriber/config.py`, and their tests. Do not create sibling modules (`pipeline.py`, `agent.py`, etc.) — other tasks own those.

## Observable Acceptance
- **Tests (pytest):** JSON and YAML fixtures parse to the identical model; missing required field → clear error; unknown/absent env var → readable error.
- **Demo:** `uv run python -c "from transcriber.config import load; print(load('examples/scartill.config.yaml'))"` prints the parsed model.
