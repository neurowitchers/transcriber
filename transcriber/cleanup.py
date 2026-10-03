"""Intermediate-artifact cleanup (``cleanup.py``).

After a recording finishes processing, its intermediate artifacts are removed
to keep the recordings directory tidy. The **durable outputs are preserved**:

- KEEP: the source ``<name>.mp4``, the final ``<name>.md`` summary, the
  ``<name>.txt`` speech-to-text transcript, and the ``<name>.slides-clean.md``
  reader-facing slide descriptions (markers stripped — the durable slides
  output).
- DELETE: ``<name>.mp3``, ``<name>.telegram.md``,
  ``<name>.slides.md`` (the markers-kept slide-description **debug**
  intermediate; its cleaned counterpart ``<name>.slides-clean.md`` is kept),
  ``<name>.notion_published.json`` (the ``agno`` publish record), the
  ``extracted_slides.<name>/`` directory (which also holds
  ``<name>.scenes.csv``), and the ``transcribe_work.<name>/`` directory (which
  holds the OpenRouter STT chunk parts ``part_*.mp3`` and the ``segments.json``
  resume manifest).

Ordering / safety contract (requirements R12):

- Cleanup runs **only on success**. It is modeled as a *separate* function the
  orchestrator calls after a recording's publish (and, when S3 is enabled, a
  successful :func:`transcriber.publish.s3.sync`) has completed. This module
  performs no upload itself; sequencing sync-before-delete is the orchestrator's
  responsibility, and :func:`cleanup` must not be called before that.
- The ``keep_intermediates`` flag (surfaced by the CLI as ``--keep-intermediates``)
  disables all deletion.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path

# Suffixes that are safe to delete after a successful run.
INTERMEDIATE_SUFFIXES = (".mp3", ".scenes.csv")

# Suffixes that must NEVER be deleted. ``.txt`` is the speech-to-text transcript
# and is preserved as a durable output alongside the ``.md`` summary.
KEEP_SUFFIXES = (".mp4", ".md", ".txt")


def intermediate_paths(recording: Path) -> list[Path]:
    """Return the set of intermediate artifact paths for ``recording``.

    Args:
        recording: The source recording path (its ``.mp4``), or any path whose
            stem identifies the recording (``<name>``). Only the stem and
            parent directory are used to derive sibling artifacts.

    The returned paths are candidates; callers should check existence before
    deleting. Never includes the ``.mp4`` source, the ``.md`` summary, or the
    ``.txt`` transcript.
    """
    recording = Path(recording)
    directory = recording.parent
    name = recording.stem  # "<name>" from "<name>.mp4"

    slides_dir = directory / f"extracted_slides.{name}"
    transcribe_work_dir = directory / f"transcribe_work.{name}"

    candidates = [
        directory / f"{name}.mp3",
        # ``<name>.txt`` (speech-to-text transcript) is intentionally NOT a
        # candidate: it is kept as a durable output.
        # The scenes CSV lives inside the extracted-slides directory, not the
        # recording dir; the slides-dir ``rmtree`` removes it, but list the
        # correct path explicitly for coverage/clarity.
        slides_dir / f"{name}.scenes.csv",
        directory / f"{name}.telegram.md",  # concise Telegram digest
        directory / f"{name}.slides.md",  # intermediate slide descriptions
        directory / f"{name}.notion_published.json",  # agno publish record
        slides_dir,  # directory
        # The OpenRouter STT work dir holds the per-chunk ``part_*.mp3`` files
        # and the ``segments.json`` resume manifest; ``rmtree`` it like the
        # extracted-slides directory.
        transcribe_work_dir,  # directory
    ]
    # Per-topic routed Telegram digests (``<name>.telegram.<topic>.md``) have
    # dynamic topic slugs, so glob for them rather than list fixed names. The
    # single ``<name>.telegram.md`` is already listed above; filter it out so it
    # is not duplicated.
    for routed in sorted(directory.glob(f"{name}.telegram.*.md")):
        if routed.name != f"{name}.telegram.md":
            candidates.append(routed)
    return candidates


def cleanup(recording: Path, keep_intermediates: bool = False) -> list[Path]:
    """Delete the intermediate artifacts for ``recording``.

    KEEPS the source ``.mp4``, the final ``.md`` summary, and the ``.txt``
    speech-to-text transcript. Deletes any of the intermediate artifacts
    (``.mp3``, ``.telegram.md``, ``.slides.md``, ``.notion_published.json``,
    the ``extracted_slides.<name>/`` directory, which holds
    ``<name>.scenes.csv``, and the ``transcribe_work.<name>/`` directory, which
    holds the OpenRouter STT ``part_*.mp3`` chunks and ``segments.json``) that
    exist.

    This function is intended to be called by the orchestrator **only after** a
    recording's successful processing — and, when S3 sync is enabled, only after
    :func:`transcriber.publish.s3.sync` has returned successfully. It performs
    no sync itself and does not verify upload state; enforcing sync-before-delete
    ordering is the caller's contract.

    Args:
        recording: The source recording path (``<name>.mp4``).
        keep_intermediates: When ``True``, no deletion occurs (debug flag).

    Returns:
        The list of paths that were actually removed (empty when
        ``keep_intermediates`` is ``True`` or nothing existed).
    """
    if keep_intermediates:
        return []

    removed: list[Path] = []
    for path in intermediate_paths(recording):
        # Defensive guard: never remove a protected output, even if a derived
        # path somehow collides with a kept suffix.
        if _has_keep_suffix(path):
            continue
        if path.is_dir():
            shutil.rmtree(path)
            removed.append(path)
        elif path.exists():
            path.unlink()
            removed.append(path)
    return removed


def _has_keep_suffix(path: Path) -> bool:
    name = path.name.lower()
    # The Telegram digests and the slide-description artifact all end in ``.md``
    # but are intermediates, not the durable summary — allow them to be deleted.
    # This covers the single ``<name>.telegram.md`` and the per-topic routed
    # digests ``<name>.telegram.<topic>.md`` (any ``.telegram*.md``), plus the
    # ``<name>.slides.md`` debug block. The durable ``<name>.slides-clean.md``
    # is NOT matched here, so it is kept.
    if re.search(r"\.telegram(\.[^.]+)?\.md$", name) or name.endswith(".slides.md"):
        return False
    return any(name.endswith(suffix) for suffix in KEEP_SUFFIXES)
