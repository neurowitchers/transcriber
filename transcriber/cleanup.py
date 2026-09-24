"""Intermediate-artifact cleanup (``cleanup.py``).

After a recording finishes processing, its intermediate artifacts are removed
to keep the recordings directory tidy. The **durable outputs are preserved**:

- KEEP: the source ``<name>.mp4`` and the final ``<name>.md`` summary.
- DELETE: ``<name>.mp3``, ``<name>.txt``,
  the ``extracted_slides.<name>/`` directory, and ``<name>.scenes.csv``.

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

import shutil
from pathlib import Path

# Suffixes that are safe to delete after a successful run.
INTERMEDIATE_SUFFIXES = (".mp3", ".txt", ".scenes.csv")

# Suffixes that must NEVER be deleted.
KEEP_SUFFIXES = (".mp4", ".md")


def intermediate_paths(recording: Path) -> list[Path]:
    """Return the set of intermediate artifact paths for ``recording``.

    Args:
        recording: The source recording path (its ``.mp4``), or any path whose
            stem identifies the recording (``<name>``). Only the stem and
            parent directory are used to derive sibling artifacts.

    The returned paths are candidates; callers should check existence before
    deleting. Never includes the ``.mp4`` source or the ``.md`` summary.
    """
    recording = Path(recording)
    directory = recording.parent
    name = recording.stem  # "<name>" from "<name>.mp4"

    candidates = [
        directory / f"{name}.mp3",
        directory / f"{name}.txt",
        directory / f"{name}.scenes.csv",
        directory / f"{name}.telegram.md",  # concise Telegram digest
        directory / f"extracted_slides.{name}",  # directory
    ]
    return candidates


def cleanup(recording: Path, keep_intermediates: bool = False) -> list[Path]:
    """Delete the intermediate artifacts for ``recording``.

    KEEPS the source ``.mp4`` and the final ``.md`` summary. Deletes any of the
    intermediate artifacts (``.mp3``, ``.txt``, ``.scenes.csv``, and
    the ``extracted_slides.<name>/`` directory) that exist.

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
    # The Telegram digest ends in ``.md`` but is an intermediate, not the
    # durable summary — allow it to be deleted.
    if name.endswith(".telegram.md"):
        return False
    return any(name.endswith(suffix) for suffix in KEEP_SUFFIXES)
