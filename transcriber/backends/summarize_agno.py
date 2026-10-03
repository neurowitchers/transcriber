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
  when set, else the default timeout — R13/E9). The blocking Notion publish runs
  in a worker thread so the timeout can still cancel the run.
* **Non-empty post-condition (R22)** — after the run, the engine applies a
  non-empty ``<name>.md`` success check: an empty or absent summary file fails
  the stage (retryable).
* **Atomicity (R21)** — summarize + Notion publish happen in a single run, so a
  Notion failure fails the whole ``summarize``+``notion`` stage.
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
    build_agno_digest_prompt,
    build_agno_summarize_prompt,
    clean_slide_descriptions,
    digest_path_for,
    slides_clean_path_for,
)
from transcriber.agent import DEFAULT_ROUTE_KEY, build_agno_route_prompt
from transcriber.backends.errors import SummarizeError
from transcriber.backends.interfaces import SummaryResult
from transcriber.backends.notion_mcp import AgnoImportError
from transcriber.backends.notion_publish import (
    DEFAULT_NOTION_TIMEOUT,
    publish_to_notion,
)
from transcriber.config import Config, DEFAULT_TIMEOUT_SECONDS, resolve_env

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

    ``timeouts.summarize`` when set, otherwise the default timeout.
    """
    if config.timeouts.summarize is not None:
        return float(config.timeouts.summarize)
    return float(DEFAULT_TIMEOUT_SECONDS)


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


def _publish_summary_and_slides(
    config: Config,
    title: str,
    summary_markdown: str,
    cleaned_slides: str,
    notion_timeout: float,
) -> str:
    """Thread entry point: publish the summary page (+ optional slides subpage).

    A thin positional-arg wrapper around
    :func:`transcriber.backends.notion_publish.publish_to_notion` so it can be
    handed to :func:`asyncio.to_thread` cleanly (``publish_to_notion`` takes
    ``slides_markdown``/``timeout`` as keyword-only args). ``cleaned_slides`` is
    already markers-stripped; an empty string means no slides child page.
    """
    return publish_to_notion(
        config,
        title,
        summary_markdown,
        slides_markdown=cleaned_slides or None,
        timeout=notion_timeout,
    )


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
        #
        # SPLIT MODEL: slide descriptions are kept OUT of the summary. The
        # summary file (<name>.md) holds only the transcript summary; the
        # cleaned, reader-facing slide descriptions (markers stripped) go to a
        # separate durable file (<name>.slides-clean.md) and, on Notion, to a
        # "Slide Descriptions" child page under the summary page. The raw
        # <name>.slides.md (markers kept) remains the debug intermediate. The
        # DIGEST stays slide-free (derived from the transcript summary only,
        # inside _run_agent).
        cleaned_slides = clean_slide_descriptions(slides_markdown)

        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(summary_text.strip() + "\n", encoding="utf-8")

        # Cleaned slides: write the durable <name>.slides-clean.md when there is
        # real slide content; otherwise CLEAR any stale one so a reprocess never
        # leaves outdated slides behind.
        slides_clean_path = slides_clean_path_for(output_path)
        if cleaned_slides:
            slides_clean_path.write_text(
                cleaned_slides + "\n", encoding="utf-8"
            )
        elif slides_clean_path.exists():
            logger.info(
                "agno summarize: no slide content, removing stale %s",
                slides_clean_path.name,
            )
            slides_clean_path.unlink()

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
            logger.info(
                "agno summarize: publishing to Notion (title=%r, slides_subpage=%s)",
                basename,
                "yes" if cleaned_slides else "no",
            )
            # Blocking urllib in a worker thread so the outer wait_for does not
            # stall the loop; the bounded per-request socket timeout is what
            # actually caps the Notion phase. The summary page carries ONLY the
            # summary; cleaned slide descriptions (when present) become a
            # "Slide Descriptions" child page under it.
            page_url = asyncio.run(
                asyncio.wait_for(
                    asyncio.to_thread(
                        _publish_summary_and_slides,
                        config,
                        basename,
                        summary_text.strip(),
                        cleaned_slides,
                        notion_timeout,
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



# --------------------------------------------------------------------------- #
# Telegram topic routing (Option B): partition one summary into per-topic +
# default digests via a SINGLE model call, parsed from strict JSON.
# --------------------------------------------------------------------------- #
def _strip_json_fence(text: str) -> str:
    """Return ``text`` with a surrounding Markdown code fence removed, if any.

    Models often wrap JSON in ```` ```json … ``` ```` despite being told not to.
    This strips a single leading fence line (``` or ```json) and a trailing
    fence line so :func:`json.loads` sees bare JSON. Non-fenced text is returned
    unchanged (modulo surrounding whitespace).
    """
    s = text.strip()
    if not s.startswith("```"):
        return s
    lines = s.splitlines()
    # Drop the opening fence line (``` or ```json).
    if lines and lines[0].lstrip().startswith("```"):
        lines = lines[1:]
    # Drop the closing fence line.
    if lines and lines[-1].strip().startswith("```"):
        lines = lines[:-1]
    return "\n".join(lines).strip()


def _parse_route_json(raw: str, config: Config) -> "dict[str, str]":
    """Parse the partition model's JSON into a ``{bucket: digest}`` mapping.

    Robust against the common model deviations:

    * a surrounding ```` ``` ````/```` ```json ```` fence (stripped);
    * missing buckets (filled with ``""`` so every configured topic + the
      default key is always present);
    * extra keys the model invents (ignored — only the configured routing
      topics and :data:`DEFAULT_ROUTE_KEY` are kept);
    * non-string values (coerced to ``""`` — treated as empty, not an error).

    Raises :class:`SummarizeError` only when the payload is not a JSON object at
    all (so the caller can fall back to the single-digest path rather than drop
    the Telegram notification entirely).

    The returned mapping always has exactly the configured topic keys plus
    :data:`DEFAULT_ROUTE_KEY`; values are stripped strings (possibly empty).
    """
    allowed = list(config.telegram.routing) + [DEFAULT_ROUTE_KEY]
    try:
        data = json.loads(_strip_json_fence(raw))
    except (ValueError, TypeError) as exc:
        raise SummarizeError(
            f"routing partition returned non-JSON output: {type(exc).__name__}"
        ) from exc
    if not isinstance(data, dict):
        raise SummarizeError(
            "routing partition JSON was not an object "
            f"(got {type(data).__name__})"
        )
    result: dict[str, str] = {}
    for key in allowed:
        value = data.get(key, "")
        result[key] = value.strip() if isinstance(value, str) else ""
    return result


async def _run_route(route_prompt: str, config: Config) -> str:
    """Run the single partition model call; return its raw text output."""
    Agent, AgnoOpenRouter = _import_agno()
    api_key = resolve_env(config.openrouter.api_key_env)
    base_url = config.openrouter.base_url
    model_id = config.openrouter.summary_model
    logger.info("agno routing: partition call (model=%s)", model_id)
    router = Agent(model=AgnoOpenRouter(id=model_id, api_key=api_key, base_url=base_url))
    out = await router.arun(route_prompt)
    return (out.get_content_as_string() or "").strip()


def build_routed_digests(config: Config, summary_markdown: str) -> "dict[str, str]":
    """Partition ``summary_markdown`` into ``{bucket: digest}`` via one model call.

    Returns a mapping whose keys are the configured ``telegram.routing`` topics
    plus :data:`DEFAULT_ROUTE_KEY`; each value is a Telegram digest for that
    bucket (possibly ``""`` when the bucket has no relevant content). The caller
    routes each non-empty bucket to ``telegram.routing[topic]`` and the
    ``DEFAULT_ROUTE_KEY`` bucket to ``telegram.default_chat_id``.

    Only meaningful when ``telegram.routing`` is non-empty; the caller gates on
    that. Raises :class:`SummarizeError` on an unusable (non-object) model
    response so the caller can fall back to the single-digest path.
    """
    timeout = _summarize_timeout(config)
    prompt = build_agno_route_prompt(config, summary_markdown)
    raw = asyncio.run(asyncio.wait_for(_run_route(prompt, config), timeout))
    buckets = _parse_route_json(raw, config)
    nonempty = sum(1 for v in buckets.values() if v)
    logger.info(
        "agno routing: partitioned into %d bucket(s), %d non-empty",
        len(buckets),
        nonempty,
    )
    return buckets
