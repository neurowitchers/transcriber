# Critique: Replace ElevenLabs transcription with OpenRouter diarized STT

**Spec under review:** `docs/specs/openrouter-transcription.md`
**Date:** 2026-10-01
**Reviewer lenses:** Product (CEO/Product Lead) + Engineering (Staff Engineer)

---

## Executive Summary

This specification proposes a clean, valuable architectural simplification: replacing the external ElevenLabs CLI dependency with OpenRouter's speech-to-text API endpoints, reusing the existing OpenRouter configuration and HTTP transport mechanisms. The problem statement is well-motivated, eliminating duplicate vendor accounts and external binary requirements.

However, the technical review identified three **must-address** issues that would cause immediate execution or runtime failure during implementation:
1. **`ffmpeg` Chunking Overlap Defect (E1 / X1):** R7 and Task 3 prescribe using `ffmpeg -f segment -segment_time` to produce audio chunks with overlap (`overlap_seconds`). The `ffmpeg` `segment` muxer does not natively support an overlap flag; attempting to pass overlap parameters to `-f segment` will fail or yield non-overlapping chunks, undermining the cross-chunk speaker reconciliation mechanism (R9).
2. **Diarization Config Contradiction (P1):** R4 mandates diarization unconditionally, while R14 introduces a configurable `transcribe.diarize: bool` flag. The specification lacks request formatting and rendering instructions when `diarize` is explicitly set to `false`.
3. **Work Directory Path Ambiguity (E2):** The spec refers generically to `work/<name>/` without anchoring it to `recordings_dir` or establishing a consistent naming schema alongside existing stages (e.g., `extracted_slides.<name>`), creating path collision and cleanup risks.

Beyond these items, several recommendations address speaker label proliferation across audio boundaries, manifest parameter validation, and pre-flight binary checks.

**Verdict:** ⚠️ **PROCEED WITH UPDATES** — All must-address items are straightforward to resolve with minor updates to the spec prior to starting implementation.

---

## Product Lens Findings

### 1a. Problem Validation
The core premise of eliminating the ElevenLabs vendor dependency and consolidating AI provider calls onto OpenRouter is well-founded. It removes friction during deployment and reduces operational overhead.

### 1b. User Value Assessment
Diarized transcription significantly improves transcript utility for downstream summarisation agents. However, the exact transcript line formatting for speaker attribution requires clear definition to ensure compatibility with prompt expectations.

### 1c. Alternative Approaches
The chunking strategy effectively bypasses OpenRouter's upstream processing timeouts (~60 s) and file size caps (25 MB). Using explicit `ffmpeg` slice commands or timestamp-based stitching provides a robust alternative to reliance on non-existent `ffmpeg` segment overlap flags.

### 1d. Edge Cases & User Experience
- **P1 (Must-Address): Contradiction between R4 and R14 on Diarization requirement.** R4 specifies that diarization is required and mandates `response_format: "verbose_json"` with provider toggles. R14 introduces `transcribe.diarize: bool` (defaulting to `true`). If an operator sets `diarize: false` (for example, when targeting models without diarization support), the spec does not detail whether provider options are omitted from the request body or how segment parsing should behave.
- **P2 (Recommendation): Output Formatting & Timestamp Line Standardisation.** R2 specifies producing a plain-text `<name>.txt` transcript with speaker attribution. Under Data Models, rendering is described as grouping consecutive same-speaker segments into `Speaker N: <text>` lines. The spec should explicitly confirm that segment timestamps (e.g., `[00:01:20]`) are excluded from `<name>.txt` to preserve consistency with the existing pipeline contract.
- **P3 (Recommendation): OpenRouter Audio Endpoint Payload Format.** The spec outlines a JSON payload with `input_audio: {"data": "<base64>", "format": "mp3"}`. While OpenRouter documents JSON base64 payloads for custom endpoints, standard OpenAI-compatible audio transcription endpoints expect `multipart/form-data`. The seam implementation should explicitly accommodate OpenRouter's JSON structure while handling HTTP headers cleanly.

### 1e. Success Measurement
Success criteria are well-covered via unit tests and cost logging verification (`usage.cost`).

---

## Engineering Lens Findings

### 2a. Architecture Soundness
- **E1 (Must-Address): `ffmpeg -f segment` does not support overlap.** R7 and Task 3 state that `ffmpeg` will cut `<name>.mp3` into parts using `-f segment -segment_time` with an overlap window (`overlap_seconds`, default `5`). The native `ffmpeg` segment muxer does not feature an overlap option. Executing `-f segment` with segment times will generate strict contiguous chunks without overlap, invalidating the overlap alignment assumptions in R9.
- **E2 (Must-Address): Directory Naming and Path Convention for Work Directory.** R7, R11, and Task 4 refer to `work/<name>/` and `work/<name>/segments.json`. To prevent collisions and align with the existing `extracted_slides.<name>` pattern in `recordings_dir`, the work directory path must be explicitly standardised as `recordings_dir / f"transcribe_work.{name}"`.

### 2b. Failure Mode Analysis
- **E4 (Recommendation): Cross-Chunk Speaker Label Proliferation.** R9 relies on matching overlapping segment text/time to align speaker labels between chunk N and chunk N+1. If a speaker is silent during the 5-second overlap window, their chunk N+1 local ID cannot be matched to their chunk N ID. Over long recordings, this results in speaker ID proliferation (e.g., `Speaker 0` through `Speaker 14`). The spec should clarify that local IDs map to a global speaker registry and document fallback handling for unmatched speakers.
- **E5 (Recommendation): Manifest Parameter Invalidation.** R11 introduces per-segment idempotency using `segments.json`. If `segment_seconds` or `overlap_seconds` is changed in config between retries of an interrupted recording, stitching old segments with newly chunked segments will corrupt timeline offsets. `segments.json` should store the active parameters and invalidate the cache if config settings change.

### 2c. Security & Privacy Review
Log hygiene specifications in R3 are thorough, correctly ensuring that base64 audio payloads, authorization headers, and API keys are excluded from exception tracebacks and logs.

### 2d. Performance & Scalability
Base64 encoding an 8-minute MP3 chunk results in approximately 10 MB of in-memory text payload per call, which is well within standard Python runtime memory limits.

### 2e. Testing Strategy
The testing plan outlined in Tasks 1–7 is comprehensive, leveraging synthetic `TranscriptChunk` fixtures and `httpx` mocks.

### 2f. Operational Readiness
- **E3 (Recommendation): Pre-flight Binary Check Removal of `elevenlabs`.** `transcriber/__main__.py` currently defines `_ALWAYS_BINARIES = ("ffmpeg", "elevenlabs")`. Tasks 5 and 7 cover CLI and dead code cleanup, but do not explicitly instruct removing `"elevenlabs"` from `_ALWAYS_BINARIES`. Leaving it in place will cause `transcriber check` to fail on environments lacking the legacy CLI binary.

### 2g. Dependencies & Integration Risks
Removing the ElevenLabs CLI binary dependency streamlines system requirements to `ffmpeg` alone for media processing.

---

## Cross-Lens Insights

- **X1 (Scope × Architecture): Audio Segmentation & Reconciliation Alignment (E1 + P1).** Resolving the `ffmpeg` chunking implementation by generating explicit time-slice calls (`ffmpeg -ss <start> -t <duration+overlap>`) directly fixes the engineering defect while ensuring that the product requirement for diarized speaker reconciliation across chunk boundaries receives valid overlapping audio segments.

---

## Findings Summary Table

| ID | Lens | Severity | Category | Finding | Suggestion |
|---|---|---|---|---|---|
| P1 | Product | 🎯 | Edge Cases & User Experience | Contradiction between mandatory diarization (R4) and configurable `diarize: bool` flag (R14) | Clarify behavior when `diarize: false`: omit provider diarization options from request body and output flat text |
| P2 | Product | 💡 | User Value Assessment | Unclear specification of timestamp headers in speaker-attributed output text | Explicitly state in R2 that `<name>.txt` uses `Speaker <ID>: <text>` lines without timestamps |
| P3 | Product | 💡 | Problem Validation | API request schema specifies JSON base64 body without referencing `multipart/form-data` compatibility | Note in Task 2 that JSON base64 format is used for OpenRouter, keeping payload headers consistent |
| E1 | Engineering | 🎯 | Architecture Soundness | `ffmpeg -f segment` does not support overlap (`overlap_seconds`), breaking chunk seam overlap (R7, Task 3) | Update R7 and Task 3 to generate overlapping chunks via explicit time-sliced `ffmpeg` calls |
| E2 | Engineering | 🎯 | Failure Modes & Paths | Ambiguous work directory location `work/<name>/` risks path collision and cleanup failure | Standardize path as `recordings_dir / f"transcribe_work.{name}"` across R7, Task 4, Task 7, and `cleanup.py` |
| E3 | Engineering | 💡 | Operational Readiness | `_ALWAYS_BINARIES` in `transcriber/__main__.py` retains `"elevenlabs"`, breaking `transcriber check` | Update Task 5 / Task 7 to remove `"elevenlabs"` from `_ALWAYS_BINARIES` in `__main__.py` |
| E4 | Engineering | 💡 | Failure Mode Analysis | Silent speakers in overlap window cause speaker ID proliferation across chunks | Document global speaker mapping heuristic and fallback logging when overlap matching is inconclusive |
| E5 | Engineering | 💡 | Failure Mode Analysis | Config parameter changes (`segment_seconds`) during retries corrupt segment stitching | Store `segment_seconds` and `overlap_seconds` in `segments.json` and invalidate manifest if parameters change |
| X1 | Both | 🎯 | Scope × Architecture | `ffmpeg` overlap defect undermines cross-chunk reconciliation feasibility | Implement explicit chunk slicing with overlap to satisfy both architectural soundness and product accuracy |

---

## Verdict

⚠️ **PROCEED WITH UPDATES** — The specification is well-targeted and achievable, but the three Must-Address items (E1, P1, E2) should be resolved in `docs/specs/openrouter-transcription.md` before implementation begins.

---

## Remediation Plan & Suggested Edits

### Proposed Edits for Must-Address Items:

1. **Fix Audio Chunking with Overlap (E1 / X1):**
   - **Update R7 & Task 3:** Replace references to `ffmpeg -f segment` with explicit time-sliced audio extraction using `ffmpeg -ss <start> -t <duration+overlap> -i <mp3> <part_path>`.
2. **Resolve Diarization Requirement Contradiction (P1):**
   - **Update R4 & Task 2:** Specify that when `transcribe.diarize` is `true`, `response_format: "verbose_json"` and provider diarization options are sent. When `transcribe.diarize` is `false`, provider diarization toggles are omitted, and segment parser outputs flat text without speaker prefixes.
3. **Standardize Work Directory Path (E2):**
   - **Update R7, R19, Task 4, and Task 7:** Define the work directory explicitly as `config.recordings_dir / f"transcribe_work.{name}"`.

### Proposed Edits for Recommendations:

4. **Confirm Plain-Text Formatting (P2):** Update R2 and Data Models section to confirm line output format is `Speaker <ID>: <text>` with no timestamp markers.
5. **Update Pre-flight Binary List (E3):** Add explicit instruction in Task 5 / Task 7 to remove `"elevenlabs"` from `_ALWAYS_BINARIES` in `transcriber/__main__.py`.
6. **Manifest Parameter Guard (E5):** Add requirement in R11 and Task 4 to record `segment_seconds` and `overlap_seconds` inside `segments.json` to prevent invalid stitch operations across parameter changes.

Would you like me to apply these changes? (all / select / none)
