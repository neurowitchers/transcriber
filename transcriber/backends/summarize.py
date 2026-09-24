"""Summarize backends: ``agy`` (default, today's behavior) and ``agno``.

Two backends implement :class:`~transcriber.backends.interfaces.SummarizeBackend`:

* :class:`AgySummarizeBackend` — drives the local ``agy`` CLI agent exactly as
  today (``transcriber.agent.run_agent``): writes ``<name>.md`` +
  ``<name>.telegram.md`` and publishes to Notion via ``agy``'s own MCP. Behavior
  is unchanged from the pre-split engine.
* ``AgnoSummarizeBackend`` — lives in :mod:`transcriber.backends.summarize_agno`
  and is imported **only when selected** (so the optional ``agno`` extra is not
  a base dependency). Use :func:`get_summarize_backend` to obtain the configured
  backend without importing ``agno`` on the default path.

Both backends produce the same artifacts and both publish Notion via **MCP**
(Spec R6/R10/R11). Selection is a pure function of ``config.summary.backend``;
there is **no silent cross-backend fallback** (Spec R19).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from transcriber.agent import digest_path_for, run_agent
from transcriber.backends.interfaces import SummarizeBackend, SummaryResult
from transcriber.config import Config

logger = logging.getLogger(__name__)


class AgySummarizeBackend:
    """Summarize via the local ``agy`` CLI agent (today's behavior, unchanged).

    Implements :class:`~transcriber.backends.interfaces.SummarizeBackend`. Wraps
    :func:`transcriber.agent.run_agent`, which writes ``<name>.md`` +
    ``<name>.telegram.md`` and publishes to Notion via ``agy``'s MCP, applying
    the non-empty-``<name>.md`` post-condition.
    """

    def summarize(
        self,
        transcript_path: Path,
        slides_markdown: Optional[str],
        recording_dir: Path,
        config: Config,
    ) -> SummaryResult:
        summary_path = run_agent(
            config,
            transcript_path,
            recording_dir,
            slides_markdown,
        )
        telegram_path = digest_path_for(summary_path)
        return SummaryResult(summary_path=summary_path, telegram_path=telegram_path)


def get_summarize_backend(config: Config) -> SummarizeBackend:
    """Return the configured summarize backend (pure function of config).

    ``summary.backend == "agy"`` (the default) returns :class:`AgySummarizeBackend`
    without importing ``agno``. ``summary.backend == "agno"`` imports the Agno
    backend lazily (so the optional ``agno`` extra is only needed when selected).

    No silent cross-backend fallback (Spec R19): an unknown value raises.
    """
    backend = config.summary.backend
    if backend == "agy":
        return AgySummarizeBackend()
    if backend == "agno":
        # Imported here so the base engine never needs the optional agno extra.
        from transcriber.backends.summarize_agno import AgnoSummarizeBackend

        return AgnoSummarizeBackend()
    raise ValueError(
        f"unknown summary.backend {backend!r} (expected 'agy' or 'agno')"
    )
