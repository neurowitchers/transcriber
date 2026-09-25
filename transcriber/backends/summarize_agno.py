"""Agno summarize backend (imported only when ``summary.backend == "agno"``).

This module drives an `Agno <https://docs.agno.com>`_ agent with a configurable
OpenRouter model to produce the meeting **summary** and a short **digest** as
plain text. The **engine** then publishes a Notion subpage by calling the Notion
REST API directly (see :mod:`transcriber.backends.notion_publish`) and writes
``<name>.md`` + ``<name>.telegram.md`` from the model output.

Design constraints (Spec R10/R11/R19/R21/R22/R22b):

* **Notion via the REST API** — the subpage is created by the engine through the
  Notion REST API, not by a model tool-call. The official Notion MCP's tool
  schemas (``oneOf``/``anyOf``/``$ref``) break tool-calling on Gemini/Mistral
  over OpenRouter (empty ``null`` completions, zero tool calls), so a
  deterministic engine-side publish is used instead.
* **Optional import** — ``agno`` and its OpenRouter model provider are imported
  lazily *inside* this module so the base engine never needs the ``agno`` extra
  unless this backend is selected. A missing import raises the actionable
  :class:`~transcriber.backends.notion_mcp.AgnoImportError`.
* **Hard time bound (R22b/E2)** — the async run is wrapped in :func:`asyncio.run`
  under a timeout equal to the summarize-stage timeout (``timeouts.summarize``
  when set, else ``timeouts.agy`` — R13/E9). The blocking Notion publish runs in
  a worker thread so the timeout can still cancel the run.
* **Non-empty post-condition (R22)** — after the run, the engine re-uses the
  same non-empty ``<name>.md`` success check as the ``agy`` path: an empty or
  absent summary file fails the stage (retryable).
* **Atomicity (R21)** — summarize + Notion publish happen in a single run, so a
  Notion failure fails the whole ``summarize``+``notion`` stage, like ``agy``.
* **Log hygiene (R16/E8)** — the OpenRouter key, the Notion token, and any
  content payload are never logged. Only stage/model/size/elapsed are logged.
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Any, Optional

from transcriber.agent import (
    build_agno_digest_prompt,
    build_agno_summarize_prompt,
    digest_path_for,
)
from transcriber.backends.errors import SummarizeError
from transcriber.backends.interfaces import SummaryResult
from transcriber.backends.notion_mcp import AgnoImportError
from transcriber.backends.notion_publish import publish_to_notion
from transcriber.config import Config, resolve_env

logger = logging.getLogger(__name__)


def _import_agno() -> "tuple[Any, Any]":
    """Import Agno's ``Agent`` + OpenRouter model lazily.

    Returns ``(Agent, OpenRouter_model_cls)``. Raises :class:`AgnoImportError`
    with an actionable install hint when the optional ``agno`` extra is not
    installed.
    """
    try:
        from agno.agent import Agent  # noqa: WPS433 (intentional lazy import)
        from agno.models.openrouter import OpenRouter as AgnoOpenRouter  # noqa: WPS433
    except ImportError as exc:  # pragma: no cover - exercised via monkeypatch
        raise AgnoImportError(
            "The 'agno' extra is required for summary.backend == 'agno' "
            "(Agno agent + OpenRouter). Install it with: "
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


async def _run_agent(
    summarize_prompt: str,
    config: Config,
    title: str,
) -> "tuple[str, Optional[str]]":
    """Produce the summary + digest with Agno, then publish via the Notion API.

    Two plain-text model calls (captured via ``get_content_as_string()``), then
    a **deterministic engine-side** Notion REST publish:

    * Phase 1 (no tools): produce the full Markdown summary.
    * Phase 1b (no tools): produce the short Telegram digest from that summary.
    * Publish: :func:`transcriber.backends.notion_publish.publish_to_notion`
      creates the subpage under the parent and writes the summary as native
      Notion blocks — no MCP, no model tool-calling (the official Notion MCP's
      tool schemas break tool-calling on Gemini/Mistral over OpenRouter).

    The engine writes ``<name>.md`` + ``<name>.telegram.md`` from the returned
    ``(summary, digest)``.
    """
    Agent, AgnoOpenRouter = _import_agno()

    # Resolve secrets at use time (by env-var name); never logged.
    api_key = resolve_env(config.openrouter.api_key_env)
    base_url = config.openrouter.base_url
    model_id = config.openrouter.summary_model

    def _model():
        return AgnoOpenRouter(id=model_id, api_key=api_key, base_url=base_url)

    # --- Phase 1: plain-text summary ----------------------------------------- #
    logger.info("agno summarize: phase 1 (summary, model=%s)", model_id)
    summarizer = Agent(model=_model())
    out = await summarizer.arun(summarize_prompt)
    summary = (out.get_content_as_string() or "").strip()
    if not summary:
        # Nothing to publish or write — let the caller fail the stage (R22).
        return "", None

    # --- Phase 1b: plain-text digest ----------------------------------------- #
    logger.info("agno summarize: phase 1b (digest, model=%s)", model_id)
    digester = Agent(model=_model())
    dout = await digester.arun(build_agno_digest_prompt(config, summary))
    digest = (dout.get_content_as_string() or "").strip() or None

    # --- Publish: engine-side direct Notion REST (no MCP, no tool-calling) --- #
    logger.info("agno summarize: publishing to Notion (title=%r)", title)
    # Runs blocking urllib in a worker thread so we don't stall the event loop
    # and the outer asyncio.wait_for timeout can still cancel it.
    await asyncio.to_thread(publish_to_notion, config, title, summary)

    return summary, digest


class AgnoSummarizeBackend:
    """Summarize via an Agno/OpenRouter model + direct Notion REST publish.

    Implements :class:`~transcriber.backends.interfaces.SummarizeBackend`. The
    model produces the summary + digest; the engine writes the files and
    publishes the Notion subpage via the REST API (no MCP).
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
        # Phase 1 prompt: summarize the inlined transcript into structured
        # output (no Notion here). Phase 2 (publish) is driven inside _run_agent
        # from the phase-1 summary. The ENGINE writes the files (the agent has
        # only the Notion MCP and cannot touch the local filesystem).
        prompt = build_agno_summarize_prompt(
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
            summary_text, digest_text = asyncio.run(
                asyncio.wait_for(_run_agent(prompt, config, basename), timeout)
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

        # The ENGINE writes the artifacts from the agent's STRUCTURED output
        # (option 1): the Agno agent has only the Notion MCP and no filesystem
        # tool, so it cannot write <name>.md / <name>.telegram.md itself.
        if not summary_text or not summary_text.strip():
            # Truncated/failed run with no usable summary → fail the stage (R22).
            raise SummarizeError(
                f"agno summarize produced no usable summary for {output_path.name}"
            )

        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(summary_text.strip() + "\n", encoding="utf-8")
        if digest_text and digest_text.strip():
            digest_path.write_text(digest_text.strip() + "\n", encoding="utf-8")

        # Non-empty <name>.md post-condition (R22): defensive re-check.
        if not output_path.exists() or not output_path.read_text(
            encoding="utf-8"
        ).strip():
            raise SummarizeError(
                f"agno did not write the expected summary file: {output_path}"
            )

        elapsed = time.monotonic() - started
        logger.info("agno summarize: complete in %.1fs", elapsed)
        return SummaryResult(summary_path=output_path, telegram_path=digest_path)
