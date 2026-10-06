# Deferred Items — installable-tool

**Date**: 2026-10-05
**Source spec**: docs/specs/installable-tool.md
**Mode**: autoflow

## Summary

Shipped the engine side of the installable-tool conversion: config auto-discovery (`--config` > `TRANSCRIBER_CONFIG` > `transcriber.config.yaml` > `config.yaml`) with strict authoritative sources and CWD anchoring, the `argparse.SUPPRESS` subparser clobber fix, `ConfigNotFoundError` in `config.py`, `load(str | Path)`, resolved-path surfacing in `--dry-run`/`check` plus an `INFO` log every run, and the promotion of `agno`/`openai` to core dependencies (with a deprecated no-op `[agno]` extra). README rewritten to the git-ref adoption model with the host-migration runbook. Intentionally left out: the actual per-host repo migrations and a few lower-priority test/CI hardening items.

## Deferred Items

| ID | Origin | Priority | Item | Rationale / Next Step |
|----|--------|----------|------|-----------------------|
| D1 | Spec R8 / Task 5 | High | Execute the per-host migration for `scartill-ai-hub`, `adsight/ai-hub`, `flyvercity/local-transcribe` (repoint `[tool.uv.sources]` to the git ref, remove submodule + `transcribe.py`, drop `[agno]`, re-lock, verify). | Out of scope for the engine PR (critique P1); run via the deploy factoid against each host once this branch lands. The runbook is in the README. |
| D2 | Code Review L2 | Low | Add a test asserting the `config: <path>` line is written to **stderr** on a failed pre-flight `check`. | Current tests cover the success branch and the resolver matrix; the stderr-on-failure assertion is a small gap. Add in a follow-up test pass. |
| D3 | Code Review L3 | Low | CI step validating prompt templates are packed into the **built wheel** (not just present in the source tree). | `tests/test_packaging.py` guards the source tree; a wheel-build inclusion check belongs in CI (which does not yet exist for this repo). Pick up when CI is introduced. |
| D4 | Critique P2 / Spec R3(release) | Medium | Cut an immutable release tag (`v0.2.0`) and switch hosts from the `branch` ref to the `tag` ref. | Deferred by design while `enhanced-pipeline` is under active development (Q3 chose branch-following now, tags later). The release step is documented in the README. |
| D5 | Critique P4 | Low | Validate the HTTPS+token git-ref form in an actual non-interactive/CI runner. | Documented in the README for CI/Docker; no CI consumes it yet, so it is untested in practice. Verify when a CI runner is added. |
| D6 | Scope (Approaches B/C) | Low | Approach B (`uv tool install`) / Approach C (private index or PyPI) as a later distribution upgrade. | Explicitly deferred in the brainstorm as non-destructive future steps once the host count or maintainer count grows. |

## Suggested Follow-up

- **Next iteration candidate:** D1 (host migrations) is the natural immediate follow-up once this branch is merged/deployed — it is the whole point of the change and is already runbooked.
- **When CI lands:** fold in D3 (wheel template-inclusion check) and D5 (HTTPS ref smoke test).
- **At stabilisation:** D4 (tag release) flips hosts onto reproducible pins; D2 is a quick test top-up to do alongside.
