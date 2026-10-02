"""Backend error types shared across the post-transcript stages.

These are defined here (Task 2) and imported by the slides / summarize
backend modules (Tasks 3/4). Keeping them in one small module avoids a
circular import between the interface definitions and the concrete backends.

**Log hygiene (R16):** error messages constructed for these types must carry
only a short, non-sensitive context (HTTP status + a truncated body snippet).
They must never embed the API key, request headers, or base64 image bytes.
"""

from __future__ import annotations


class SlideDescribeError(RuntimeError):
    """Raised when the ``describe_slides`` stage's backend fails.

    Covers transport failures, non-2xx responses (after retries), malformed
    JSON, and missing ``choices[0].message.content``. A deliberately empty
    backend result is *not* an error (see R9) — it resolves to an empty
    ``<name>.slides.md``.
    """


class SummarizeError(RuntimeError):
    """Raised when the ``summarize`` stage's backend fails.

    Raised by the ``agno`` summarize backend. Kept in this shared error-type
    module so the error types are owned in one place.
    """


class TranscribeError(RuntimeError):
    """Raised when the ``transcribe`` stage's OpenRouter STT backend fails.

    Covers transport failures, non-2xx responses (after retries, e.g. a
    ``400`` from a model that cannot diarize — no silent fallback, R10), and
    malformed JSON. Same log-hygiene contract as the sibling errors: messages
    carry only a short, non-sensitive context (HTTP status + a truncated body
    snippet) and never embed the API key, request headers, or base64 audio
    bytes.
    """
