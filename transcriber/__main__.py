"""Console entry point + orchestrator for the transcriber CLI.

Wires together the independently-implemented stage modules into a single batch
run:

    pipeline -> describe_slides -> summarize (+ Notion) -> Telegram -> S3 -> cleanup

The two post-transcript stages (``describe_slides`` and ``summarize``) each run
a backend selected purely from config (``stages.slides.backend`` /
``summary.backend``). Backends default to ``agy`` so an unchanged config keeps
today's behavior. Paid/network calls live only in these manifest-gated
orchestrator steps — the ``pipeline`` stage stays media-only (ffmpeg /
scenedetect / transcribe).

Usage::

    transcriber [--config PATH] [--keep-intermediates] [--dry-run]
    transcriber check [--config PATH]

Design highlights:

* **Pre-flight check (E2):** required binaries must be on ``PATH`` and required
  env vars set before any work starts. ``transcriber check`` runs this
  standalone (exit 0 when all present, non-zero + message otherwise).
* **``--dry-run`` (P1):** print the execution plan and exit — no subprocesses,
  no agent, no publishing, no deletion.
* **Per-recording state manifest (E1):** already-completed stages are skipped on
  retry (no duplicate Notion pages / Telegram sends / re-sync).
* **Failure isolation:** a raised exception in one recording is caught, logged,
  recorded in that recording's manifest, and the batch continues. Cleanup runs
  only on a recording's full success.
"""

from __future__ import annotations

import argparse
import logging
import shutil
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Sequence

from transcriber import agent as agent_mod
from transcriber import cleanup as cleanup_mod
from transcriber import pipeline as pipeline_mod
from transcriber import state as state_mod
from transcriber import backends as backends_mod
from transcriber.config import Config, MissingEnvVarError, load, resolve_env
from transcriber.publish import s3 as s3_mod
from transcriber.publish.telegram import TelegramPublisher

logger = logging.getLogger("transcriber")

DEFAULT_CONFIG = "config.yaml"


# --------------------------------------------------------------------------- #
# Backend selection (pure functions of config)
# --------------------------------------------------------------------------- #
def _get_slides_backend(config: Config):
    """Return the configured ``describe_slides`` backend instance.

    Selection is a pure function of ``config.stages.slides.backend``; there is
    no silent cross-backend fallback (Spec R19): an unknown value raises.
    """
    backend = config.stages.slides.backend
    if backend == "agy":
        return backends_mod.AgySlidesBackend()
    if backend == "openrouter":
        return backends_mod.OpenRouterSlidesBackend()
    raise ValueError(
        f"unknown stages.slides.backend {backend!r} (expected 'agy' or 'openrouter')"
    )


def _uses_agy(config: Config) -> bool:
    """Return ``True`` when any enabled stage uses the ``agy`` CLI."""
    if config.stages.slides.enabled and config.stages.slides.backend == "agy":
        return True
    if config.summary.backend == "agy":
        return True
    return False


def _needs_openrouter(config: Config) -> bool:
    """Return ``True`` when a stage requires the OpenRouter API key."""
    slides_openrouter = (
        config.stages.slides.enabled
        and config.stages.slides.backend == "openrouter"
    )
    return slides_openrouter or config.summary.backend == "agno"


# --------------------------------------------------------------------------- #
# Pre-flight check (E2)
# --------------------------------------------------------------------------- #
# Binaries required regardless of post-transcript backend selection.
_ALWAYS_BINARIES = ("ffmpeg", "elevenlabs")


def _required_binaries(config: Config) -> list[str]:
    """Return the list of binaries that must be on PATH for ``config``."""
    required = list(_ALWAYS_BINARIES)
    if config.stages.slides.enabled:
        required.append("scenedetect")
    # ``agy`` is required only when a post-transcript stage actually uses it.
    if _uses_agy(config):
        required.append("agy")
    if s3_mod.is_enabled(config):
        required.append("aws")
    return required


def _required_env_vars(config: Config) -> list[str]:
    """Return the list of env-var names that must be set for ``config``.

    Referenced by name only — secret values are never read here (Spec R16).
    """
    # Telegram token is always needed to disseminate.
    required = [config.telegram.bot_token_env]

    # OpenRouter key iff slides=openrouter or summary=agno.
    if _needs_openrouter(config) and config.openrouter is not None:
        required.append(config.openrouter.api_key_env)

    # Notion token iff summary=agno (agno launches the Notion MCP itself).
    if config.summary.backend == "agno" and config.notion.token_env:
        required.append(config.notion.token_env)

    return required


def preflight_check(config: Config) -> list[str]:
    """Return a list of human-readable problems (empty when all is well).

    Checks required binaries on ``PATH`` (via :func:`shutil.which`) and required
    environment variables per enabled stages.
    """
    problems: list[str] = []

    for binary in _required_binaries(config):
        if shutil.which(binary) is None:
            problems.append(f"missing required binary on PATH: '{binary}'")

    for var_name in _required_env_vars(config):
        try:
            resolve_env(var_name)
        except MissingEnvVarError:
            problems.append(f"missing required environment variable: '{var_name}'")

    return problems


# --------------------------------------------------------------------------- #
# Discovery
# --------------------------------------------------------------------------- #
def _summary_path(mp4: Path, config: Config) -> Path:
    """Return the final summary ``.md`` path for a recording."""
    name = mp4.stem
    output_name = config.agent.output_file.format(basename=name)
    output_path = Path(output_name)
    if not output_path.is_absolute():
        output_path = mp4.parent / output_name
    return output_path


def discover_new_recordings(config: Config) -> list[Path]:
    """Return NEW ``*.mp4`` recordings (summary ``.md`` does not yet exist).

    Sorted by filename for deterministic ordering.
    """
    recordings_dir = Path(config.recordings_dir)
    if not recordings_dir.exists():
        return []
    new: list[Path] = []
    for mp4 in sorted(recordings_dir.glob("*.mp4")):
        if not _summary_path(mp4, config).exists():
            new.append(mp4)
    return new


def _transcript_path(mp4: Path, config: Config) -> Path:
    """Return the transcript path handed to the agent (the ``.txt``)."""
    name = mp4.stem
    return mp4.parent / f"{name}.txt"


def _slides_md_path(mp4: Path, config: Config) -> Path:
    """Return the ``<name>.slides.md`` path written by ``describe_slides``."""
    return mp4.parent / f"{mp4.stem}.slides.md"


def _slide_image_paths(mp4: Path, config: Config) -> list[str]:
    """Return slide image paths when slides were extracted, else empty."""
    if not config.stages.slides.enabled:
        return []
    slides_dir = Path(config.recordings_dir) / f"extracted_slides.{mp4.stem}"
    if not slides_dir.exists():
        return []
    return [str(p) for p in sorted(slides_dir.glob("*")) if p.is_file()]


# --------------------------------------------------------------------------- #
# Execution plan (dry-run)
# --------------------------------------------------------------------------- #
def _enabled_stage_names(config: Config) -> list[str]:
    stages = ["audio-extract"]
    if config.stages.slides.enabled:
        stages.append("scene-extract")
    stages.append("transcribe")
    if config.stages.slides.enabled:
        stages.append(f"describe-slides [{config.stages.slides.backend}]")
    stages.append(f"summarize+notion [{config.summary.backend}]")
    stages.append("telegram")
    if s3_mod.is_enabled(config):
        stages.append("s3-sync")
    return stages


def _telegram_chats(config: Config) -> list[str]:
    chats = [config.telegram.default_chat_id]
    for chat_id in config.telegram.routing.values():
        if chat_id not in chats:
            chats.append(chat_id)
    return chats


def print_plan(config: Config, recordings: Sequence[Path]) -> None:
    """Print the dry-run execution plan; performs NO side effects."""
    lines: list[str] = ["Execution plan (dry-run):"]

    if not recordings:
        lines.append("  no new recordings found in "
                     f"{config.recordings_dir}")
    else:
        stage_list = ", ".join(_enabled_stage_names(config))
        for mp4 in recordings:
            lines.append(f"  recording: {mp4.name}")
            lines.append(f"    stages: {stage_list}")

        lines.append("")
        lines.append("Publishing destinations:")
        lines.append(
            f"  Notion: server='{config.notion.server}', "
            f"parent_page_id='{config.notion.parent_page_id}'"
        )
        lines.append(
            "  Telegram chats: " + ", ".join(_telegram_chats(config))
        )
        if s3_mod.is_enabled(config) and config.s3 is not None:
            lines.append(f"  S3: bucket='{config.s3.bucket}', "
                         f"profile='{config.s3.profile}'")
        else:
            lines.append("  S3: disabled")

    print("\n".join(lines))


# --------------------------------------------------------------------------- #
# Batch execution
# --------------------------------------------------------------------------- #
@dataclass
class RecordingOutcome:
    name: str
    succeeded: bool
    failed_stage: Optional[str] = None
    error: Optional[str] = None


def _process_one(
    mp4: Path,
    config: Config,
    *,
    keep_intermediates: bool,
) -> RecordingOutcome:
    """Run all stages for a single recording, honoring the state manifest.

    Raises are converted into a failed :class:`RecordingOutcome` by the caller;
    here we let them propagate so the failing stage can be recorded.
    """
    state = state_mod.RecordingState(mp4)
    name = mp4.stem
    current_stage = "pipeline"
    try:
        # 1. Pipeline (deterministic media stages).
        current_stage = "pipeline"
        if state.is_complete("pipeline"):
            logger.info("[%s] pipeline: already complete, skipping", name)
        else:
            logger.info("[%s] pipeline: running", name)
            pipeline_mod.process_recording(mp4, config)
            state.mark_complete("pipeline")

        # 2. Describe slides (manifest-gated; only when slides enabled).
        #    Runs the configured slides backend and writes <name>.slides.md.
        current_stage = "describe_slides"
        slides_md_path = _slides_md_path(mp4, config)
        if not config.stages.slides.enabled:
            logger.info("[%s] describe_slides: disabled, skipping", name)
        elif state.is_complete("describe_slides"):
            logger.info(
                "[%s] describe_slides: already complete, skipping", name
            )
        elif slides_md_path.exists():
            # Idempotent skip: the artifact already exists (e.g. from a prior
            # crash after write but before mark). Do not re-issue the paid call.
            logger.info(
                "[%s] describe_slides: %s already present, skipping backend",
                name,
                slides_md_path.name,
            )
            state.mark_complete("describe_slides")
        else:
            backend = _get_slides_backend(config)
            slides = backends_mod.build_slide_inputs(mp4.parent, name)
            transcript_text = _transcript_path(mp4, config).read_text(
                encoding="utf-8"
            )
            slides_timeout = float(config.timeouts.slides)
            logger.info(
                "[%s] describe_slides: running backend=%s slides=%d",
                name,
                config.stages.slides.backend,
                len(slides),
            )
            started = time.monotonic()
            slides_markdown = backend.describe(
                slides,
                transcript_text,
                config,
                timeout=slides_timeout,
            )
            elapsed = time.monotonic() - started
            # An empty slide result writes an empty <name>.slides.md and still
            # completes (Constraints).
            slides_md_path.write_text(slides_markdown, encoding="utf-8")
            logger.info(
                "[%s] describe_slides: backend=%s wrote %d chars in %.2fs",
                name,
                config.stages.slides.backend,
                len(slides_markdown),
                elapsed,
            )
            state.mark_complete("describe_slides")

        # 3. Summarize + Notion (single backend run).
        current_stage = "summarize"
        if state.is_complete("summarize") and state.is_complete("notion"):
            logger.info("[%s] summarize: already complete, skipping", name)
        else:
            logger.info(
                "[%s] summarize: running backend=%s",
                name,
                config.summary.backend,
            )
            # Read the prepared slide markdown (empty/whitespace -> treated as
            # no slides by the backend). Absent file -> None. When slides are
            # disabled, never feed a stale <name>.slides.md into the summary —
            # treat it exactly like slides-off (Spec R3).
            slides_markdown: Optional[str] = None
            if config.stages.slides.enabled and slides_md_path.exists():
                slides_markdown = slides_md_path.read_text(encoding="utf-8")
            summarize_backend = backends_mod.get_summarize_backend(config)
            summarize_backend.summarize(
                _transcript_path(mp4, config),
                slides_markdown,
                mp4.parent,
                config,
            )
            state.mark_complete("summarize")
            state.mark_complete("notion")

        # 4. Telegram dissemination.
        current_stage = "telegram"
        if state.is_complete("telegram"):
            logger.info("[%s] telegram: already complete, skipping", name)
        else:
            logger.info("[%s] telegram: sending", name)
            summary_file = _summary_path(mp4, config)
            digest_file = agent_mod.digest_path_for(summary_file)
            # Prefer the concise digest agy wrote for chat; fall back to the
            # full summary only if the digest is missing/empty.
            if digest_file.exists() and digest_file.read_text(encoding="utf-8").strip():
                message_text = digest_file.read_text(encoding="utf-8")
            else:
                logger.warning(
                    "[%s] telegram: digest missing/empty, sending full summary",
                    name,
                )
                message_text = summary_file.read_text(encoding="utf-8")
            TelegramPublisher(config).send(message_text)
            state.mark_complete("telegram")

        # 5. S3 sync (gated).
        current_stage = "s3"
        if not s3_mod.is_enabled(config):
            logger.info("[%s] s3: disabled, skipping", name)
        elif state.is_complete("s3"):
            logger.info("[%s] s3: already complete, skipping", name)
        else:
            logger.info("[%s] s3: syncing", name)
            s3_mod.sync(mp4.parent, config)
            state.mark_complete("s3")

        # 6. Cleanup (only on full success, unless kept).
        current_stage = "cleanup"
        if keep_intermediates:
            logger.info("[%s] cleanup: skipped (--keep-intermediates)", name)
        elif state.is_complete("cleanup"):
            logger.info("[%s] cleanup: already complete, skipping", name)
        else:
            logger.info("[%s] cleanup: removing intermediates", name)
            cleanup_mod.cleanup(mp4, keep_intermediates=keep_intermediates)
            state.mark_complete("cleanup")

        return RecordingOutcome(name=name, succeeded=True)
    except Exception as exc:  # noqa: BLE001 - failure isolation is intentional
        logger.exception("[%s] failed at stage '%s': %s", name, current_stage, exc)
        return RecordingOutcome(
            name=name,
            succeeded=False,
            failed_stage=current_stage,
            error=str(exc),
        )


def run_batch(
    config: Config,
    recordings: Sequence[Path],
    *,
    keep_intermediates: bool = False,
) -> list[RecordingOutcome]:
    """Process each recording with failure isolation; return per-recording outcomes."""
    outcomes: list[RecordingOutcome] = []
    for mp4 in recordings:
        outcome = _process_one(mp4, config, keep_intermediates=keep_intermediates)
        outcomes.append(outcome)
    return outcomes


def _summarize_batch(outcomes: Sequence[RecordingOutcome]) -> str:
    """Return a one-line batch summary."""
    succeeded = sum(1 for o in outcomes if o.succeeded)
    failed = [o for o in outcomes if not o.succeeded]
    parts = [f"{succeeded} succeeded"]
    if failed:
        detail = ", ".join(f"{o.name} failed at {o.failed_stage}" for o in failed)
        parts.append(f"{len(failed)} failed ({detail})")
    return "; ".join(parts)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="transcriber",
        description="Local-first meeting transcriber engine.",
    )
    parser.add_argument(
        "--config",
        default=DEFAULT_CONFIG,
        help="Path to the config file (.json/.yaml/.yml).",
    )
    parser.add_argument(
        "--keep-intermediates",
        action="store_true",
        help="Do not delete intermediate artifacts after a successful run.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the execution plan and exit without side effects.",
    )

    subparsers = parser.add_subparsers(dest="command")
    check = subparsers.add_parser(
        "check",
        help="Run the pre-flight check (binaries + env vars) and exit.",
    )
    check.add_argument(
        "--config",
        default=DEFAULT_CONFIG,
        help="Path to the config file (.json/.yaml/.yml).",
    )
    return parser


def _configure_logging() -> None:
    if not logging.getLogger().handlers:
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        )


def _cmd_check(config: Config) -> int:
    problems = preflight_check(config)
    # Surface the selected backend per post-transcript stage (R24).
    slides_desc = (
        f"describe_slides backend='{config.stages.slides.backend}'"
        if config.stages.slides.enabled
        else "describe_slides: disabled"
    )
    summarize_desc = f"summarize backend='{config.summary.backend}'"
    if problems:
        print("Pre-flight check FAILED:", file=sys.stderr)
        print(f"  {slides_desc}", file=sys.stderr)
        print(f"  {summarize_desc}", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1
    print("Pre-flight check passed: all required binaries and env vars present.")
    print(f"  {slides_desc}")
    print(f"  {summarize_desc}")
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Console entry point mapped to ``transcriber`` in ``pyproject.toml``."""
    _configure_logging()
    parser = _build_parser()
    args = parser.parse_args(argv)

    try:
        config = load(args.config)
    except Exception as exc:  # config errors -> clear message, non-zero exit
        print(f"transcriber: failed to load config '{args.config}': {exc}",
              file=sys.stderr)
        return 2

    # `transcriber check` subcommand.
    if args.command == "check":
        return _cmd_check(config)

    # Dry-run: plan only, no pre-flight enforcement, no side effects.
    if args.dry_run:
        recordings = discover_new_recordings(config)
        print_plan(config, recordings)
        return 0

    # Fail fast on pre-flight problems before doing any real work.
    problems = preflight_check(config)
    if problems:
        print("Pre-flight check FAILED:", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1

    recordings = discover_new_recordings(config)
    if not recordings:
        logger.info("no new recordings found in %s", config.recordings_dir)
        print("transcriber: no new recordings to process.")
        return 0

    logger.info("processing %d new recording(s)", len(recordings))
    outcomes = run_batch(
        config,
        recordings,
        keep_intermediates=args.keep_intermediates,
    )

    summary = _summarize_batch(outcomes)
    logger.info("batch complete: %s", summary)
    print(f"transcriber: {summary}")

    # Non-zero exit when any recording failed (batch still ran to completion).
    return 0 if all(o.succeeded for o in outcomes) else 1


if __name__ == "__main__":
    raise SystemExit(main())
