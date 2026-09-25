"""Backend interfaces for the two post-transcript stages.

Two small, pure interfaces (typing ``Protocol``s) selected per-stage by config:

* :class:`SlidesBackend` — turns extracted slide images (+ transcript context)
  into a ``<name>.slides.md`` markdown block.
* :class:`SummarizeBackend` — turns the transcript (+ the slide markdown) into
  ``<name>.md`` + ``<name>.telegram.md`` and publishes to Notion.

Concrete backends (agy / openrouter / agno) live in Tasks 3/4. No stage wiring
happens here (that is Task 5); these are pure, testable contracts.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Protocol, Sequence, runtime_checkable

from transcriber.config import Config


@dataclass
class SlideInput:
    """A single extracted slide plus its scene timing.

    Attributes:
        image_path: Path to the slide JPEG.
        timestamp: Human-readable ``MM:SS - MM:SS`` scene range, or the string
            ``"unknown"`` when the timing could not be resolved from the scenes
            CSV. Timing is never omitted (R8).
    """

    image_path: Path
    timestamp: str


@dataclass
class SummaryResult:
    """Paths to the two files a summarize backend writes.

    Both files are written by the backend; the engine reads them back
    (file-is-source-of-truth contract).
    """

    summary_path: Path  # <name>.md
    telegram_path: Path  # <name>.telegram.md


@runtime_checkable
class SlidesBackend(Protocol):
    """Produce the ``<name>.slides.md`` markdown from slide inputs."""

    def describe(
        self,
        slides: Sequence[SlideInput],
        transcript_text: str,
        config: Config,
        *,
        timeout: float,
    ) -> str:
        """Return the slide-description markdown.

        An empty slide set or a valid empty backend response both resolve to
        ``""`` (R9); a transport/format failure raises
        :class:`~transcriber.backends.errors.SlideDescribeError`.
        """
        ...


@runtime_checkable
class SummarizeBackend(Protocol):
    """Produce ``<name>.md`` + ``<name>.telegram.md`` and publish to Notion.

    Notion publishing is **backend-specific** (not always MCP):

    * ``agy`` — publishes via ``agy``'s own Notion MCP as part of its run.
    * ``agno`` — the engine publishes the subpage directly via the Notion REST
      API (:func:`transcriber.backends.notion_publish.publish_to_notion`); no
      MCP, no model tool-calling.
    """

    def summarize(
        self,
        transcript_path: Path,
        slides_markdown: Optional[str],
        recording_dir: Path,
        config: Config,
    ) -> SummaryResult:
        """Write both files and publish the Notion subpage.

        ``slides_markdown`` is embedded when non-empty and omitted when
        ``None``/empty/whitespace (R3). The Notion publish path is
        backend-specific: the ``agy`` backend publishes via MCP; the ``agno``
        backend publishes directly via the Notion REST API (see the class
        docstring).
        """
        ...
