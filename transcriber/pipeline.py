"""Deterministic media pipeline.

Ports the deterministic media stages from the original host PowerShell
transcription script to Python using the :mod:`duct` library:

1. **Audio extract** — ``ffmpeg`` mp4 -> mp3.
2. **Slide/scene extraction** (optional, when ``config.stages.slides``) —
   ``scenedetect``.
3. **Transcribe** — ``elevenlabs speech-to-text convert`` -> ``<name>.jsonl``.
4. **Parse transcript** (optional, when ``config.stages.parse_transcript``) —
   :func:`transcriber.parse.parse_jsonl_transcript` -> ``<name>.txt``.

Every child-process call is wrapped with a configurable timeout drawn from
``config.timeouts``. Each stage is idempotent: a step whose output already
exists is skipped. The pipeline returns a structured per-recording result
listing which artifacts are new.

Windows-first path handling (all paths are :class:`pathlib.Path`).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Optional

import duct

from transcriber import parse

if TYPE_CHECKING:  # pragma: no cover - typing only
    from transcriber.config import Config


# Poll interval (seconds) used while waiting for a child process under a
# timeout. Small enough to enforce timeouts promptly, large enough to avoid
# busy-spinning.
_POLL_INTERVAL_SECONDS = 0.1


class PipelineTimeoutError(RuntimeError):
    """Raised when a child process exceeds its configured timeout."""

    def __init__(self, argv: list[str], timeout: float) -> None:
        self.argv = list(argv)
        self.timeout = timeout
        super().__init__(
            f"Command timed out after {timeout}s: {' '.join(str(a) for a in argv)}"
        )


@dataclass
class RecordingResult:
    """Per-recording result describing which artifacts were newly produced.

    Attributes:
        name: The recording base name (stem of the ``.mp4``).
        mp4: Path to the source ``.mp4``.
        new_artifacts: Names of artifacts produced during this run (a subset of
            ``{"mp3", "slides", "jsonl", "txt"}``), in stage order.
        skipped_artifacts: Names of artifacts that already existed and were
            skipped.
    """

    name: str
    mp4: Path
    new_artifacts: list[str] = field(default_factory=list)
    skipped_artifacts: list[str] = field(default_factory=list)


def _run_command(
    argv: list[str],
    timeout: float,
    stdout_path: Optional[Path] = None,
) -> None:
    """Run a single child process via :mod:`duct`, enforcing a timeout.

    This is the sole seam through which the pipeline invokes external binaries.
    Tests monkeypatch this function to avoid spawning real processes.

    Args:
        argv: The full command line (program plus arguments).
        timeout: Maximum wall-clock seconds to allow before killing the child
            and raising :class:`PipelineTimeoutError`.
        stdout_path: When provided, the child's stdout is redirected to this
            file (mirrors the shell ``>`` redirection used for transcription).

    Raises:
        PipelineTimeoutError: if the child does not finish within ``timeout``.
        duct.StatusError: if the child exits with a non-zero status.
    """
    expression = duct.cmd(*argv)
    if stdout_path is not None:
        expression = expression.stdout_path(str(stdout_path))

    handle = expression.start()
    deadline = time.monotonic() + timeout
    while True:
        output = handle.poll()
        if output is not None:
            return
        if time.monotonic() >= deadline:
            handle.kill()
            handle.wait()
            raise PipelineTimeoutError(argv, timeout)
        time.sleep(_POLL_INTERVAL_SECONDS)


# --------------------------------------------------------------------------- #
# Stage helpers
# --------------------------------------------------------------------------- #
def _extract_audio(mp4: Path, mp3: Path, timeout: float) -> None:
    """Extract audio from ``mp4`` into ``mp3`` via ffmpeg.

    Mirrors the source flags:
    ``ffmpeg -hide_banner -loglevel error -i <mp4> -vn -c:a libmp3lame -q:a 2 <mp3>``.
    """
    argv = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(mp4),
        "-vn",
        "-c:a",
        "libmp3lame",
        "-q:a",
        "2",
        str(mp3),
    ]
    _run_command(argv, timeout)


def _extract_slides(
    mp4: Path,
    slides_dir: Path,
    scenes_csv: str,
    timeout: float,
) -> None:
    """Extract slides/scenes from ``mp4`` via scenedetect.

    Mirrors the source command:
    ``scenedetect -b pyav -i <mp4> -o extracted_slides.<name>
    detect-content --threshold 30 --min-scene-len 5s
    list-scenes -f <name>.scenes.csv save-images -n 1``.
    """
    argv = [
        "scenedetect",
        "-b",
        "pyav",
        "-i",
        str(mp4),
        "-o",
        str(slides_dir),
        "detect-content",
        "--threshold",
        "30",
        "--min-scene-len",
        "5s",
        "list-scenes",
        "-f",
        scenes_csv,
        "save-images",
        "-n",
        "1",
    ]
    _run_command(argv, timeout)


def _transcribe(mp3: Path, jsonl: Path, model_id: str, timeout: float) -> None:
    """Transcribe ``mp3`` into ``jsonl`` via the elevenlabs CLI.

    Mirrors the source command (stdout redirected to the ``.jsonl`` file):
    ``elevenlabs speech-to-text convert --file <mp3>
    --model-id <model_id> --format jsonl > <name>.jsonl``.
    """
    argv = [
        "elevenlabs",
        "speech-to-text",
        "convert",
        "--file",
        str(mp3),
        "--model-id",
        model_id,
        "--format",
        "jsonl",
    ]
    _run_command(argv, timeout, stdout_path=jsonl)


# --------------------------------------------------------------------------- #
# Per-recording driver
# --------------------------------------------------------------------------- #
def process_recording(mp4: Path, config: "Config") -> RecordingResult:
    """Process a single ``.mp4`` recording through the deterministic stages.

    Each stage is idempotent: a step whose output already exists is skipped.

    Args:
        mp4: Path to the source ``.mp4``.
        config: The loaded configuration model.

    Returns:
        A :class:`RecordingResult` describing which artifacts are new.
    """
    mp4 = Path(mp4)
    name = mp4.stem
    directory = mp4.parent
    recordings_dir = Path(config.recordings_dir)

    mp3 = directory / f"{name}.mp3"
    jsonl = directory / f"{name}.jsonl"
    txt = directory / f"{name}.txt"
    slides_dir = recordings_dir / f"extracted_slides.{name}"
    scenes_csv = f"{name}.scenes.csv"

    result = RecordingResult(name=name, mp4=mp4)

    # 1. Audio extract (mp4 -> mp3). ``have_mp3`` tracks availability for the
    # downstream transcribe stage: an artifact produced this run counts even if
    # the guard cannot re-``stat`` it (e.g. under a mocked child process).
    if mp3.exists():
        result.skipped_artifacts.append("mp3")
        have_mp3 = True
    else:
        _extract_audio(mp4, mp3, config.timeouts.ffmpeg)
        result.new_artifacts.append("mp3")
        have_mp3 = True

    # 2. Slide/scene extraction (optional).
    if config.stages.slides:
        if slides_dir.exists():
            result.skipped_artifacts.append("slides")
        else:
            _extract_slides(mp4, slides_dir, scenes_csv, config.timeouts.scenedetect)
            result.new_artifacts.append("slides")

    # 3. Transcribe (mp3 -> jsonl). Requires the mp3 to be available.
    have_jsonl = jsonl.exists()
    if have_jsonl:
        result.skipped_artifacts.append("jsonl")
    elif have_mp3:
        _transcribe(mp3, jsonl, config.transcribe.model_id, config.timeouts.elevenlabs)
        result.new_artifacts.append("jsonl")
        have_jsonl = True

    # 4. Parse transcript (optional; jsonl -> txt).
    if config.stages.parse_transcript:
        if txt.exists():
            result.skipped_artifacts.append("txt")
        elif have_jsonl:
            parse.parse_jsonl_transcript(jsonl, txt)
            result.new_artifacts.append("txt")

    return result


def process_all(config: "Config") -> list[RecordingResult]:
    """Process every ``.mp4`` under ``config.recordings_dir``.

    Args:
        config: The loaded configuration model.

    Returns:
        A list of :class:`RecordingResult`, one per discovered ``.mp4``,
        sorted by file name for deterministic ordering.
    """
    recordings_dir = Path(config.recordings_dir)
    results: list[RecordingResult] = []
    for mp4 in sorted(recordings_dir.glob("*.mp4")):
        results.append(process_recording(mp4, config))
    return results
