"""Per-recording stage-status manifest (``state.py``).

Each recording gets a sidecar JSON manifest (``<name>.transcriber_state.json``)
living next to its ``.mp4``. The manifest records which pipeline stages have
completed so that a retry can **skip** already-finished stages — this is what
prevents duplicate Notion pages, duplicate Telegram sends, or a redundant S3
re-sync when a batch is re-run after a partial failure.

The manifest — not artifact existence alone — is the source of truth for stage
completion (requirement E1).

Tracked stages (in execution order):

    pipeline, describe_slides, summarize, notion, telegram, s3, cleanup

``describe_slides`` runs (when ``stages.slides.enabled``) the configured slides
backend to produce ``<name>.slides.md`` before summarize consumes it.

``notion`` is folded into the ``summarize`` stage in practice (both summarize
backends publish to Notion as part of the same run), but it is tracked
separately so a future split does not break the manifest schema.

Pre-existing manifests written before ``describe_slides`` existed simply lack
that key. Because completion is read as ``_completed.get(stage, False)``, an
absent ``describe_slides`` is treated as *incomplete*, so re-running a recording
whose manifest predates this stage re-issues the slides call exactly once (and
subsequently records it, making further re-runs idempotent).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable

# Canonical ordered list of stages tracked by the manifest.
STAGES: tuple[str, ...] = (
    "pipeline",
    "describe_slides",
    "summarize",
    "notion",
    "telegram",
    "s3",
    "cleanup",
)

# Sidecar filename suffix (appended to the recording stem).
_SUFFIX = ".transcriber_state.json"


def manifest_path(recording: Path) -> Path:
    """Return the manifest path for ``recording`` (its ``.mp4``).

    The manifest lives next to the recording as
    ``<name>.transcriber_state.json``.
    """
    recording = Path(recording)
    return recording.parent / f"{recording.stem}{_SUFFIX}"


class RecordingState:
    """Read/write wrapper over a single recording's stage manifest.

    The manifest is a small JSON document::

        {"name": "<name>", "completed": {"pipeline": true, ...}}

    Only stages that are marked complete are stored as ``True``. Any stage not
    present is treated as incomplete.
    """

    def __init__(self, recording: Path) -> None:
        self._recording = Path(recording)
        self._path = manifest_path(self._recording)
        self._completed: dict[str, bool] = {}
        self._load()

    # ------------------------------------------------------------------ #
    # Persistence
    # ------------------------------------------------------------------ #
    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            # A corrupt/unreadable manifest is treated as "nothing complete";
            # re-running is safe because individual stages are idempotent or
            # gated by their own checks.
            self._completed = {}
            return
        completed = data.get("completed", {}) if isinstance(data, dict) else {}
        if isinstance(completed, dict):
            self._completed = {
                stage: bool(done)
                for stage, done in completed.items()
                if stage in STAGES
            }

    def save(self) -> None:
        """Persist the manifest to disk (atomic-ish write)."""
        payload = {
            "name": self._recording.stem,
            "completed": self._completed,
        }
        self._path.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    # ------------------------------------------------------------------ #
    # Queries / mutations
    # ------------------------------------------------------------------ #
    @property
    def path(self) -> Path:
        return self._path

    def is_complete(self, stage: str) -> bool:
        """Return ``True`` when ``stage`` has been recorded as complete."""
        return self._completed.get(stage, False)

    def mark_complete(self, stage: str) -> None:
        """Mark ``stage`` complete and persist immediately.

        Persisting after each stage means a crash between stages still leaves an
        accurate manifest for the next retry.
        """
        if stage not in STAGES:
            raise ValueError(f"unknown stage: {stage!r}")
        self._completed[stage] = True
        self.save()

    def completed_stages(self) -> list[str]:
        """Return completed stages in canonical execution order."""
        return [s for s in STAGES if self._completed.get(s)]

    def remaining_stages(self, stages: Iterable[str] | None = None) -> list[str]:
        """Return the stages (of ``stages`` or all) not yet complete."""
        candidates = tuple(stages) if stages is not None else STAGES
        return [s for s in candidates if not self._completed.get(s)]

    def delete(self) -> None:
        """Remove the manifest file if it exists (used after full success)."""
        if self._path.exists():
            self._path.unlink()
