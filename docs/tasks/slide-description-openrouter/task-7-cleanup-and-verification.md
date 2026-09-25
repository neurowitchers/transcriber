# Task 7 — Cleanup (`<name>.slides.md` + scenes-CSV path) + full verification

**Status:** [x]

**Spec:** `docs/specs/slide-description-openrouter.md`
**Dependencies:** Tasks 1–6 (verification runs against the full feature). The
cleanup edits depend only on Task 3's artifact (`<name>.slides.md`).

## Target
`transcriber/cleanup.py` and `tests/test_cleanup.py`; then a full-suite +
manual parity run.

## Change
- `cleanup.py`: add `".slides.md"` handling. Because it ends in `.md`, add a
  `_has_keep_suffix` special-case (like `.telegram.md`) so `<name>.slides.md` is
  deletable while the durable `<name>.md` is kept; add it to `intermediate_paths`;
  confirm `--keep-intermediates` preserves it.
- **Fix the scenes-CSV path:** `intermediate_paths` currently lists
  `directory/<name>.scenes.csv` (recording dir) where the file never exists.
  Correct it to `extracted_slides.<name>/<name>.scenes.csv`, or drop the stale
  recording-dir candidate and rely on the slides-dir `rmtree`.

## Constraints
- Never delete `<name>.mp4` or the durable `<name>.md`.
- Keep the sync-before-delete ordering contract intact.

## Ownership
Owns: `cleanup.py` and `tests/test_cleanup.py`, plus running the final
verification.

## Observable Acceptance
- **Tests (pytest):** cleanup deletes `<name>.slides.md`, keeps `<name>.md` and
  `<name>.mp4`; `--keep-intermediates` preserves `<name>.slides.md`; the
  corrected scenes-CSV path is covered.
- **Demo:** `uv run pytest -q` fully green. **Manual parity check (success bar):**
  2–3 real slides-on recordings of differing slide counts (one near the R23
  ceiling), slides `openrouter` + summarize `agy` (and separately + summarize
  `agno`); confirm ≥40% wall-time reduction vs. all-`agy`; eyeball the Slide
  Descriptions + Notion page for parity; record token/$-cost as a secondary
  metric.
