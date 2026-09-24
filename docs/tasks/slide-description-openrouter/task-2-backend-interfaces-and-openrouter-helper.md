# Task 2 — Backend interfaces + OpenRouter vision-call helper

**Status:** [ ]

**Spec:** `docs/specs/slide-description-openrouter.md`
**Dependencies:** Task 1 (config model)

## Target
`transcriber/backends/` (new package): `__init__.py`, interface definitions, and
the OpenRouter HTTP helper used by the **slides** vision call. Plus
`tests/test_backends.py`.

## Change
- Define the two backend Protocols:
  - `SlidesBackend.describe(slides, transcript_text, config, *, timeout) -> str`
  - `SummarizeBackend.summarize(transcript_path, slides_markdown, recording_dir,
    config) -> SummaryResult` (paths to `<name>.md` + `<name>.telegram.md`).
- Implement `_openrouter_vision(config, messages, model, *, timeout) -> str`:
  - Headers `Authorization: Bearer <key>` (+ `HTTP-Referer`, `X-Title`).
  - `httpx` POST to `{base_url}/chat/completions`, honoring `timeout`.
  - Retry `429`/`5xx` with bounded exponential backoff (small capped attempts),
    then raise `SlideDescribeError`.
  - On non-2xx (after retries), malformed JSON, or missing
    `choices[0].message.content` → `SlideDescribeError`.
- Define error types `SlideDescribeError` (and `SummarizeError` for Task 4).

## Constraints
- **Log hygiene:** error messages/logs carry status + a short body snippet only —
  never the key, headers, or base64 image bytes.
- The helper is for the **slides** vision call only; the `agno` summarize path
  does not use it (it goes through Agno — Task 4/6).
- No stage wiring here (that is Task 5). Pure, testable functions.

## Ownership
Owns: `transcriber/backends/__init__.py`, the interfaces module, the OpenRouter
helper, and `tests/test_backends.py`. Coordinate the shared error-type module
with Task 3/4 (define here, import there).

## Observable Acceptance
- **Tests (pytest, mock `httpx`):**
  - Helper sends `Authorization`+`HTTP-Referer`+`X-Title`, correct URL, model.
  - Retry `429`→`200` succeeds (patch sleep); persistent `5xx` → error after cap.
  - Malformed JSON / missing content → error.
  - Errors contain status + snippet only (no secrets/bytes).
- **Demo:** drive the helper against a mocked transport for success, retry, and
  failure.
