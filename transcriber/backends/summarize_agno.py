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
import json
import logging
import time
from pathlib import Path
from typing import Any, Optional

from transcriber.agent import (
    append_slide_descriptions,
    build_agno_digest_prompt,
    build_agno_summarize_prompt,
    digest_path_for,
)
from transcriber.backends.errors import SummarizeError
from transcriber.backends.interfaces import SummaryResult
from transcriber.backends.notion_mcp import AgnoImportError
from transcriber.backends.notion_publish import (
    DEFAULT_NOTION_TIMEOUT,
    publish_to_notion,
)
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


def _notion_request_timeout(config: Config) -> float:
    """Bounded per-request Notion socket timeout for the publish phase.

    Capped by the summarize-stage budget so a single stuck request can never
    outlive the stage; falls back to the module default when that budget is
    larger (a request should fail fast, not consume the whole budget).
    """
    return min(_summarize_timeout(config), DEFAULT_NOTION_TIMEOUT)


async def _run_agent(
    summarize_prompt: str,
    config: Config,
) -> "tuple[str, Optional[str]]":
    """Produce the summary + digest with Agno (no Notion publish here).

    Two plain-text model calls (captured via ``get_content_as_string()``):

    * Phase 1 (no tools): produce the full Markdown summary.
    * Phase 1b (no tools): produce the short Telegram digest from that summary.

    Notion publishing is deliberately **not** done here. The caller writes the
    local artifacts (``<name>.md`` / ``<name>.telegram.md``) **first**, then
    publishes via :func:`transcriber.backends.notion_publish.publish_to_notion`.
    Persisting artifacts before publishing means a crash after a successful
    publish (but before the manifest is marked) leaves the local files intact,
    so a retry does not regenerate the summary and create a duplicate Notion
    page (see :func:`_publish_record_path`).

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

    return summary, digest


def _publish_record_path(summary_path: Path) -> Path:
    """Sidecar recording that the Notion subpage was already published.

    ``<dir>/<name>.md`` -> ``<dir>/<name>.notion_published.json``. Its presence
    means a prior run already created the Notion page for this summary, so a
    retry (after a crash between publish and the manifest mark) must NOT publish
    again — otherwise it would create a duplicate page.
    """
    return summary_path.with_suffix(".notion_published.json")


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

        # The ENGINE writes the artifacts from the model output: the Agno agent
        # has no filesystem tool, so it cannot write <name>.md / .telegram.md.
        if not summary_text or not summary_text.strip():
            # Truncated/failed run with no usable summary → fail the stage (R22).
            raise SummarizeError(
                f"agno summarize produced no usable summary for {output_path.name}"
            )

        # --- Persist artifacts BEFORE publishing (R21 / duplicate-page fix) --- #
        # Writing the local files first means a crash *after* a successful
        # Notion publish (but before the manifest is marked) still leaves valid
        # local artifacts, so a retry regenerates nothing and — guarded by the
        # publish record below — does not create a duplicate Notion page.
        # Build the FINAL summary document: the model-produced transcript
        # summary with the prepared slide descriptions appended by the ENGINE
        # (deterministic concatenation). The model is told not to reproduce the
        # slide block (see transcriber.agent._slide_block), so slide content
        # never consumes the model's bounded output budget — which previously
        # truncated the summary before the Slide Descriptions section was
        # reached. An absent/empty <name>.slides.md is a no-op (treated as
        # slides-off). The DIGEST intentionally stays slide-free (it is derived
        # from the transcript summary only, inside _run_agent).
        final_document = append_slide_descriptions(summary_text, slides_markdown)

        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(final_document, encoding="utf-8")

        # Digest: write when non-empty, otherwise CLEAR any stale digest so a
        # retry/reprocess never disseminates an old digest. The telegram stage
        # falls back to the full summary when the digest file is missing.
        if digest_text and digest_text.strip():
            digest_path.write_text(digest_text.strip() + "\n", encoding="utf-8")
        elif digest_path.exists():
            logger.info(
                "agno summarize: empty digest, removing stale %s",
                digest_path.name,
            )
            digest_path.unlink()

        # Non-empty <name>.md post-condition (R22): defensive re-check.
        if not output_path.exists() or not output_path.read_text(
            encoding="utf-8"
        ).strip():
            raise SummarizeError(
                f"agno did not write the expected summary file: {output_path}"
            )

        # --- Publish to Notion (idempotent) ---------------------------------- #
        # Skip if a prior run already created the page (crash-between-publish-
        # and-mark recovery): re-publishing would create a duplicate subpage.
        record_path = _publish_record_path(output_path)
        if record_path.exists():
            logger.info(
                "agno summarize: Notion page already published (%s), skipping "
                "publish to avoid a duplicate",
                record_path.name,
            )
        else:
            notion_timeout = _notion_request_timeout(config)
            logger.info("agno summarize: publishing to Notion (title=%r)", basename)
            # Blocking urllib in a worker thread so the outer wait_for does not
            # stall the loop; the bounded per-request socket timeout is what
            # actually caps the Notion phase.
            page_url = asyncio.run(
                asyncio.wait_for(
                    asyncio.to_thread(
                        publish_to_notion,
                        config,
                        basename,
                        final_document,
                        timeout=notion_timeout,
                    ),
                    timeout,
                )
            )
            # Record the successful publish so a retry never duplicates it.
            record_path.write_text(
                json.dumps({"url": page_url}) + "\n", encoding="utf-8"
            )

        elapsed = time.monotonic() - started
        logger.info("agno summarize: complete in %.1fs", elapsed)
        return SummaryResult(summary_path=output_path, telegram_path=digest_path)
