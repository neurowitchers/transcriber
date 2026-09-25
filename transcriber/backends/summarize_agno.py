"""Agno summarize backend (imported only when ``summary.backend == "agno"``).

This module drives an `Agno <https://docs.agno.com>`_ agent with a configurable
OpenRouter model and the **official Notion MCP server attached as a tool**. The
agent is given the *same* summary/digest+Notion prompt as the ``agy`` backend
(reused verbatim from :mod:`transcriber.agent`), so both summarize backends
share one prompt contract and produce the same observable outputs:
``<name>.md`` + ``<name>.telegram.md`` plus a published Notion subpage.

Design constraints (Spec R10/R11/R19/R21/R22/R22b):

* **Notion via MCP only** — the Notion subpage is created through the official
  Notion MCP server (launched by :mod:`transcriber.backends.notion_mcp`), never
  a hand-rolled REST client (R11).
* **Optional import** — ``agno`` and its OpenRouter model provider are imported
  lazily *inside* this module so the base engine never needs the ``agno`` extra
  unless this backend is selected. A missing import raises the actionable
  :class:`~transcriber.backends.notion_mcp.AgnoImportError`.
* **Hard time bound + subprocess cleanup (R22b/E2/E3)** — the async agent run is
  wrapped in :func:`asyncio.run` under a timeout equal to the summarize-stage
  timeout (``timeouts.summarize`` when set, else ``timeouts.agy`` — R13/E9).
  ``MCPTools`` is opened as an async context manager and therefore closed on
  **every** path (success, error, and timeout), so the spawned Notion MCP
  subprocess (``npx``/node) is never orphaned.
* **Non-empty post-condition (R22)** — after the run, the engine re-uses the
  same non-empty ``<name>.md`` success check as the ``agy`` path: an empty or
  absent summary file fails the stage (retryable). A truncated model turn thus
  fails rather than persisting a partial summary.
* **Agent-run atomicity (R21)** — summary write + Notion publish happen inside a
  single agent run, so a Notion failure fails the whole ``summarize``+``notion``
  stage, exactly like ``agy``.
* **Log hygiene (R16/E8)** — the OpenRouter key, the Notion token, and any
  content payload are never logged. Only stage/model/size/elapsed are logged.
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Any, Optional

from transcriber.agent import build_agno_prompt, digest_path_for, parse_agno_response
from transcriber.backends.errors import SummarizeError
from transcriber.backends.interfaces import SummaryResult
from transcriber.backends.notion_mcp import AgnoImportError, notion_mcp_tools
from transcriber.config import Config, resolve_env

logger = logging.getLogger(__name__)


def _import_agno() -> "tuple[Any, Any]":
    """Import Agno's ``Agent`` and OpenRouter model lazily.

    Returns ``(Agent, OpenRouter_model_cls)``. Raises
    :class:`AgnoImportError` with an actionable install hint when the optional
    ``agno`` extra is not installed.
    """
    try:
        from agno.agent import Agent  # noqa: WPS433 (intentional lazy import)
        from agno.models.openrouter import OpenRouter as AgnoOpenRouter  # noqa: WPS433
    except ImportError as exc:  # pragma: no cover - exercised via monkeypatch
        raise AgnoImportError(
            "The 'agno' extra is required for summary.backend == 'agno' "
            "(Agno agent + OpenRouter + Notion MCP). Install it with: "
            "`uv sync --extra agno` (or `pip install 'transcriber[agno]'`)."
        ) from exc
    return Agent, AgnoOpenRouter


def _summarize_timeout(config: Config) -> float:
    """The summarize-stage hard time bound (R13/E9).

    ``timeouts.summarize`` when set, otherwise falls back to ``timeouts.agy``.
    """
    if config.timeouts.summarize is not None:
        return float(config.timeouts.summarize)
    return float(config.timeouts.agy)


async def _run_agent(prompt: str, config: Config) -> str:
    """Run the Agno agent with the Notion MCP attached, then close it.

    Returns the agent's final message text (the engine parses it for the
    summary + digest blocks and writes the files — the Agno agent has only the
    Notion MCP and cannot write local files).

    ``MCPTools`` is entered as an async context manager so it is closed on the
    success, exception, and (via the caller's timeout cancellation) timeout
    paths — no orphaned Notion MCP subprocess (R22b/E3).
    """
    Agent, AgnoOpenRouter = _import_agno()

    # Resolve secrets at use time (by env-var name); never logged.
    api_key = resolve_env(config.openrouter.api_key_env)
    base_url = config.openrouter.base_url
    model_id = config.openrouter.summary_model

    mcp = notion_mcp_tools(config)  # unconnected; we own its lifecycle here.
    async with mcp:
        model = AgnoOpenRouter(id=model_id, api_key=api_key, base_url=base_url)
        agent = Agent(model=model, tools=[mcp])
        logger.info(
            "agno summarize: invoking Notion MCP publish via agent (model=%s)",
            model_id,
        )
        output = await agent.arun(prompt)
        return output.get_content_as_string() or ""


class AgnoSummarizeBackend:
    """Summarize via an Agno agent (OpenRouter model + Notion MCP).

    Implements :class:`~transcriber.backends.interfaces.SummarizeBackend`. Same
    file-is-source-of-truth + Notion-via-MCP contract as the ``agy`` backend.
    """

    def summarize(
        self,
        transcript_path: Path,
        slides_markdown: Optional[str],
        recording_dir: Path,
        config: Config,
    ) -> SummaryResult:
        transcript_path = Path(transcript_path)
        recording_dir = Path(recording_dir)

        basename = transcript_path.stem
        output_name = config.agent.output_file.format(basename=basename)
        output_path = Path(output_name)
        if not output_path.is_absolute():
            output_path = recording_dir / output_name
        output_path = output_path.resolve()

        digest_path = digest_path_for(output_path)

        # Agno-specific prompt: transcript inlined (the agent has no file-read
        # tool), agent publishes to Notion via MCP and RETURNS the summary +
        # digest as sentinel-delimited text. The ENGINE writes the files from
        # the returned text (the agent cannot touch the local filesystem).
        prompt = build_agno_prompt(
            config,
            transcript_path.resolve(),
            slides_markdown,
        )

        timeout = _summarize_timeout(config)
        started = time.monotonic()
        transcript_len = (
            transcript_path.stat().st_size if transcript_path.exists() else 0
        )
        logger.info(
            "agno summarize: model=%s transcript_bytes=%d slides=%s timeout=%.0fs",
            config.openrouter.summary_model,
            transcript_len,
            "yes" if (slides_markdown and slides_markdown.strip()) else "no",
            timeout,
        )

        try:
            response = asyncio.run(
                asyncio.wait_for(_run_agent(prompt, config), timeout)
            )
        except AgnoImportError:
            # Actionable "install the agno extra" error — surface as-is (R14/R19).
            raise
        except asyncio.TimeoutError as exc:
            # Hard time bound hit (R22b). asyncio.wait_for cancels the task,
            # which propagates CancelledError into the `async with mcp` body so
            # MCPTools is closed on the timeout path too (E3).
            elapsed = time.monotonic() - started
            raise SummarizeError(
                f"agno summarize timed out after {elapsed:.0f}s "
                f"(limit {timeout:.0f}s)"
            ) from exc
        except SummarizeError:
            raise
        except Exception as exc:  # noqa: BLE001 - wrap any backend failure (R19)
            # No content in the message (log hygiene R16/E8) — only the type.
            raise SummarizeError(
                f"agno summarize failed: {type(exc).__name__}"
            ) from exc

        # The ENGINE writes the artifacts from the agent's returned text
        # (option 1): the Agno agent has only the Notion MCP and no filesystem
        # tool, so it cannot write <name>.md / <name>.telegram.md itself.
        try:
            summary_text, digest_text = parse_agno_response(response)
        except ValueError as exc:
            # Truncated/malformed response with no usable summary → fail the
            # stage (R22). Do not log the response body (R16/E8).
            raise SummarizeError(
                f"agno summarize produced no usable summary block: {exc}"
            ) from exc

        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(summary_text.strip() + "\n", encoding="utf-8")
        if digest_text and digest_text.strip():
            digest_path.write_text(digest_text.strip() + "\n", encoding="utf-8")

        # Non-empty <name>.md post-condition (R22): defensive — parse guarantees
        # a non-empty summary, but re-check the written file.
        if not output_path.exists() or not output_path.read_text(
            encoding="utf-8"
        ).strip():
            raise SummarizeError(
                f"agno did not write the expected summary file: {output_path}"
            )

        elapsed = time.monotonic() - started
        logger.info("agno summarize: complete in %.1fs", elapsed)
        return SummaryResult(summary_path=output_path, telegram_path=digest_path)
