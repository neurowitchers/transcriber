"""Pluggable backends for the two post-transcript stages.

Public surface (Task 2):
    * Interfaces: :class:`SlidesBackend`, :class:`SummarizeBackend`, and the
      :class:`SlideInput` / :class:`SummaryResult` data types.
    * Errors: :class:`SlideDescribeError`, :class:`SummarizeError`.
    * The shared OpenRouter vision helper :func:`_openrouter_vision` (used by
      the slides ``openrouter`` backend in Task 3).

Concrete backends and stage wiring: the ``openrouter`` slides backend
(:class:`OpenRouterSlidesBackend`) and the summarize backends (``agy`` /
``agno``).
"""

from __future__ import annotations

from transcriber.backends.errors import SlideDescribeError, SummarizeError
from transcriber.backends.interfaces import (
    SlideInput,
    SlidesBackend,
    SummarizeBackend,
    SummaryResult,
)
from transcriber.backends.openrouter import _openrouter_vision
from transcriber.backends.slides import (
    OpenRouterSlidesBackend,
    build_slide_inputs,
)
from transcriber.backends.summarize import (
    AgySummarizeBackend,
    get_summarize_backend,
)

__all__ = [
    "SlideDescribeError",
    "SummarizeError",
    "SlideInput",
    "SlidesBackend",
    "SummarizeBackend",
    "SummaryResult",
    "_openrouter_vision",
    "OpenRouterSlidesBackend",
    "build_slide_inputs",
    "AgySummarizeBackend",
    "get_summarize_backend",
]
