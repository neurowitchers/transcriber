"""Summarize backend selection.

The ``summarize`` stage runs on the single ``agno`` backend:

* ``AgnoSummarizeBackend`` — lives in :mod:`transcriber.backends.summarize_agno`
  and is imported **only when selected** (so the optional ``agno`` extra is not
  a base dependency). It produces the summary + digest with an Agno/OpenRouter
  model, then the **engine** publishes the Notion subpage directly via the
  Notion REST API (:func:`transcriber.backends.notion_publish.publish_to_notion`)
  — no MCP, no model tool-calling. Use :func:`get_summarize_backend` to obtain
  the configured backend without importing ``agno`` on an unrelated path.

Selection is a pure function of ``config.summary.backend``; there is **no
silent cross-backend fallback** (Spec R19).
"""

from __future__ import annotations

import logging

from transcriber.backends.interfaces import SummarizeBackend
from transcriber.config import Config

logger = logging.getLogger(__name__)


def get_summarize_backend(config: Config) -> SummarizeBackend:
    """Return the configured summarize backend (pure function of config).

    ``summary.backend == "agno"`` imports the Agno backend lazily (so the
    optional ``agno`` extra is only needed when selected).

    No silent cross-backend fallback (Spec R19): an unknown value raises.
    """
    backend = config.summary.backend
    if backend == "agno":
        # Imported here so the base engine never needs the optional agno extra.
        from transcriber.backends.summarize_agno import AgnoSummarizeBackend

        return AgnoSummarizeBackend()
    raise ValueError(
        f"unknown summary.backend {backend!r} (expected 'agno')"
    )
