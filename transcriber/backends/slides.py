"""Slides ``describe_slides`` backend + the slide-input helper.

This module implements the ``describe_slides`` backend (``openrouter`` is the
only slides backend):

* :class:`OpenRouterSlidesBackend` — issues **one OpenRouter vision call per
  slide** (via the :func:`_openrouter_vision` helper), turning each extracted
  slide JPEG into part of the ``<name>.slides.md`` markdown block. The
  descriptor is **image-only**: it never receives the transcript. Transcript
  cross-referencing is the summarize stage's job.

Plus :func:`build_slide_inputs`, which lists the extracted slide JPEGs in a
deterministic order and joins each to its scene timing parsed from the scenes
CSV that scenedetect writes **into the slides dir**
(``extracted_slides.<name>/<name>.scenes.csv`` — E7), never a ``.mp4`` sibling.

Design invariants (Spec R7/R8/R9/R16/R17/R23):

* **Empty semantics (R9):** an empty slide set → ``""`` with **no** HTTP call; a
  valid empty/whitespace backend response → ``""`` (not an error). A
  transport/format failure raises
  :class:`~transcriber.backends.errors.SlideDescribeError`.
* **One call per slide:** each slide is sent in its own vision call carrying a
  single image and no transcript; the per-slide markdown is concatenated.
* **Per-slide timestamps (R8):** each slide's message carries a ``Timestamp:
  MM:SS - MM:SS`` text part placed **immediately before** its ``image_url``
  part. Unknown timing is passed as the literal ``unknown`` — never omitted.
* **Downscale (R25):** each JPEG is downscaled to a bounded long edge before
  base64 to control size/cost. This is a size control, **not** redaction.
* **Deterministic ceiling (R23):** module constants cap the max slide count and
  the max total encoded bytes; exceeding either raises ``SlideDescribeError``
  **before** any HTTP call. Over-ceiling decks hard-fail (no chunking).
* **Log hygiene (R16/R17):** logs carry stage/model/slide-count/elapsed only —
  never the API key, headers, or image bytes.
* **No pipeline coupling:** the backend is not called from
  ``pipeline.process_recording`` — the pipeline stays ffmpeg + scenedetect +
  transcribe only. It runs in the manifest-gated orchestrator step.
"""

from __future__ import annotations

import base64
import csv
import logging
import time
from pathlib import Path
from typing import Sequence

from transcriber.backends.errors import SlideDescribeError
from transcriber.backends.interfaces import SlideInput
from transcriber.backends.openrouter import _openrouter_vision
from transcriber.config import Config

logger = logging.getLogger(__name__)

# Templates dir (shared with the agent stage) for the slide_extractor rules.
_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "prompt_templates"

# Downscale each slide JPEG to at most this long edge (px) before base64. A
# size/cost control, NOT redaction (R25).
MAX_IMAGE_LONG_EDGE = 1024

# JPEG re-encode quality after downscale.
_JPEG_QUALITY = 85

# Deterministic payload ceiling (R23). Set generously so it only trips on
# genuinely pathological inputs; over-ceiling decks HARD-FAIL before any send.
# MAX_SLIDES is the DEFAULT — a host may override it via openrouter.max_slides.
MAX_SLIDES = 60
MAX_TOTAL_ENCODED_BYTES = 48 * 1024 * 1024  # 48 MiB of base64 image data

# Filename suffix of the scenes CSV inside the slides dir (E7).
_SCENES_CSV_SUFFIX = ".scenes.csv"


# --------------------------------------------------------------------------- #
# Slide-input helper
# --------------------------------------------------------------------------- #
def _slides_dir(recording_dir: Path, name: str) -> Path:
    return Path(recording_dir) / f"extracted_slides.{name}"


def _list_slide_images(slides_dir: Path) -> list[Path]:
    """Return the slide JPEGs in a deterministic (sorted-by-name) order."""
    if not slides_dir.is_dir():
        return []
    images = [
        p
        for p in slides_dir.iterdir()
        if p.is_file() and p.suffix.lower() in (".jpg", ".jpeg")
    ]
    return sorted(images, key=lambda p: p.name)


def _format_timecode(raw: str) -> str | None:
    """Normalize a scenedetect timecode (``HH:MM:SS.mmm``) to ``MM:SS``.

    Returns ``None`` when the value cannot be parsed.
    """
    raw = raw.strip()
    if not raw:
        return None
    # Drop fractional seconds.
    core = raw.split(".", 1)[0]
    parts = core.split(":")
    try:
        nums = [int(p) for p in parts]
    except ValueError:
        return None
    if len(nums) == 3:
        h, m, s = nums
    elif len(nums) == 2:
        h, m, s = 0, nums[0], nums[1]
    elif len(nums) == 1:
        h, m, s = 0, 0, nums[0]
    else:
        return None
    total_minutes = h * 60 + m
    return f"{total_minutes:02d}:{s:02d}"


def _parse_scenes_csv(csv_path: Path) -> list[str]:
    """Parse the scenes CSV into ordered ``MM:SS - MM:SS`` range strings.

    scenedetect's ``list-scenes -f`` output prepends a cut-list row, then a
    header row (``Scene Number, Start Frame, Start Timecode, ...``), then one
    row per scene. We locate the header by its ``Start Timecode`` / ``End
    Timecode`` columns so we are robust to the leading cut-list row.

    Returns an empty list when the file is missing or unparseable (the caller
    then emits ``unknown`` for every slide and logs a warning — R8).
    """
    try:
        text = csv_path.read_text(encoding="utf-8")
    except OSError:
        return []

    rows = list(csv.reader(text.splitlines()))
    header_idx = None
    start_col = end_col = None
    for i, row in enumerate(rows):
        lowered = [c.strip().lower() for c in row]
        if "start timecode" in lowered and "end timecode" in lowered:
            header_idx = i
            start_col = lowered.index("start timecode")
            end_col = lowered.index("end timecode")
            break

    if header_idx is None or start_col is None or end_col is None:
        return []

    ranges: list[str] = []
    for row in rows[header_idx + 1 :]:
        if len(row) <= max(start_col, end_col):
            continue
        start = _format_timecode(row[start_col])
        end = _format_timecode(row[end_col])
        if start is None or end is None:
            ranges.append("unknown")
        else:
            ranges.append(f"{start} - {end}")
    return ranges


def build_slide_inputs(recording_dir: Path | str, name: str) -> list[SlideInput]:
    """List the extracted slide JPEGs joined to their scene timing.

    Args:
        recording_dir: The directory holding the recording (and the
            ``extracted_slides.<name>/`` slides dir beside it).
        name: The recording base name (the ``.mp4`` stem).

    Returns:
        Slide inputs in deterministic filename order. Each carries a
        ``MM:SS - MM:SS`` timestamp parsed from
        ``extracted_slides.<name>/<name>.scenes.csv``; when the CSV is
        missing/unparseable or shorter than the image count, the affected
        slides get the literal ``"unknown"`` (R8) and a warning is logged so the
        degraded path is visible. Zero images → ``[]``.
    """
    recording_dir = Path(recording_dir)
    slides_dir = _slides_dir(recording_dir, name)
    images = _list_slide_images(slides_dir)
    if not images:
        return []

    # E7: the scenes CSV lives in the slides dir, NOT beside the .mp4.
    csv_path = slides_dir / f"{name}{_SCENES_CSV_SUFFIX}"
    ranges = _parse_scenes_csv(csv_path)

    if not ranges:
        logger.warning(
            "describe_slides: scenes CSV missing or unparseable at %s; "
            "emitting 'unknown' timing for all %d slide(s)",
            csv_path,
            len(images),
        )

    inputs: list[SlideInput] = []
    for i, image in enumerate(images):
        if i < len(ranges) and ranges[i] and ranges[i] != "unknown":
            timestamp = ranges[i]
        else:
            if ranges:
                logger.warning(
                    "describe_slides: no timing for slide %s (index %d); "
                    "using 'unknown'",
                    image.name,
                    i,
                )
            timestamp = "unknown"
        inputs.append(SlideInput(image_path=image, timestamp=timestamp))
    return inputs


# --------------------------------------------------------------------------- #
# Prompt / message building (OpenRouter backend)
# --------------------------------------------------------------------------- #
def _read_extractor_rules() -> str:
    return (_TEMPLATES_DIR / "slide_extractor.md").read_text(encoding="utf-8")


def _leading_text(config: Config) -> str:
    """Build the leading text part: extractor rules + language (image-only).

    The slide descriptor is **image-only**: it describes what is visually
    present on the slide and never receives the transcript. Transcript
    cross-referencing (speaker commentary, decisions, Q&A) is the summarize
    stage's job, which has the full transcript plus this slide markdown.
    """
    from transcriber.agent import _language_instruction

    rules = _read_extractor_rules()
    language = _language_instruction(config.summary.language)
    return (
        "You are generating the **Slide Descriptions** markdown block for a "
        "recorded meeting. Output ONLY the slide-description markdown (no "
        "preamble, no summary sections).\n\n"
        f"Write the descriptions in {language}\n\n"
        "The slide image below is preceded by its `Timestamp: MM:SS - MM:SS` "
        "scene range. Describe only what is visually present on the slide. "
        "Follow these per-slide extraction rules exactly:\n\n"
        f"{rules}"
    )


def _downscale_jpeg_to_bytes(image_path: Path, max_long_edge: int) -> bytes:
    """Decode ``image_path``, downscale to ``max_long_edge``, re-encode as JPEG.

    Uses OpenCV (``cv2``), a hard transitive dependency via ``scenedetect``.
    Kept as a module-level seam so tests can monkeypatch it without real images.
    Never logs image contents (R16).

    Raises:
        SlideDescribeError: if the image cannot be read/decoded.
    """
    import cv2  # transitive via scenedetect -> opencv-python
    import numpy as np

    data = image_path.read_bytes()
    arr = np.frombuffer(data, dtype=np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if img is None:
        raise SlideDescribeError(
            f"could not decode slide image: {image_path.name}"
        )
    h, w = img.shape[:2]
    long_edge = max(h, w)
    if long_edge > max_long_edge:
        scale = max_long_edge / float(long_edge)
        new_size = (max(1, int(round(w * scale))), max(1, int(round(h * scale))))
        img = cv2.resize(img, new_size, interpolation=cv2.INTER_AREA)
    ok, buf = cv2.imencode(
        ".jpg", img, [int(cv2.IMWRITE_JPEG_QUALITY), _JPEG_QUALITY]
    )
    if not ok:
        raise SlideDescribeError(
            f"could not re-encode slide image: {image_path.name}"
        )
    return buf.tobytes()


def _encode_slide(slide: SlideInput) -> str:
    """Downscale + base64-encode one slide's JPEG (R25). Never logs bytes."""
    jpeg_bytes = _downscale_jpeg_to_bytes(slide.image_path, MAX_IMAGE_LONG_EDGE)
    return base64.b64encode(jpeg_bytes).decode("ascii")


def _preflight_deck_encoded_bytes(
    config: Config, slides: Sequence[SlideInput]
) -> dict[Path, str]:
    """Encode every slide once and enforce the **whole-deck** byte ceiling (R23).

    Returns a ``{image_path: base64}`` cache so the per-slide message build does
    not re-encode. Raises ``SlideDescribeError`` if the deck's *total* encoded
    size exceeds :data:`MAX_TOTAL_ENCODED_BYTES` — **before** any HTTP call, so
    an over-ceiling deck hard-fails before a single image is sent (the per-slide
    loop must not reset the accumulator per slide).
    """
    cache: dict[Path, str] = {}
    total_encoded = 0
    for slide in slides:
        b64 = _encode_slide(slide)
        cache[slide.image_path] = b64
        total_encoded += len(b64)
        if total_encoded > MAX_TOTAL_ENCODED_BYTES:
            raise SlideDescribeError(
                "slide deck exceeds the maximum encoded payload of "
                f"{MAX_TOTAL_ENCODED_BYTES} bytes; raise the ceiling, split "
                "the recording, or disable the slides stage for this host"
            )
    return cache


def _build_single_slide_message(
    config: Config,
    slide: SlideInput,
    *,
    encoded_cache: dict[Path, str],
) -> tuple[list[dict], int]:
    """Build the user message for **one** slide and return (messages, bytes).

    The slide descriptor is image-only: the message carries the extractor rules
    + language (no transcript), one ``Timestamp:`` text part, and one
    ``image_url``. The base64 image is taken from ``encoded_cache`` (populated by
    :func:`_preflight_deck_encoded_bytes`, which already enforced the whole-deck
    byte ceiling before any HTTP call), so images are never re-encoded here.
    """
    b64 = encoded_cache[slide.image_path]
    content: list[dict] = [
        {"type": "text", "text": _leading_text(config)},
        {"type": "text", "text": f"Timestamp: {slide.timestamp}"},
        {
            "type": "image_url",
            "image_url": {"url": f"data:image/jpeg;base64,{b64}"},
        },
    ]
    messages = [{"role": "user", "content": content}]
    return messages, len(b64)


# --------------------------------------------------------------------------- #
# Backend
# --------------------------------------------------------------------------- #
class OpenRouterSlidesBackend:
    """``describe_slides`` via one OpenRouter vision call **per slide**.

    Each slide is sent in its own image-only chat-completions call (no
    transcript context); the per-slide markdown is concatenated into the
    ``<name>.slides.md`` block. Transcript cross-referencing is the summarize
    stage's responsibility, which receives the full transcript plus this block.
    """

    def describe(
        self,
        slides: Sequence[SlideInput],
        transcript_text: str,
        config: Config,
        *,
        timeout: float,
    ) -> str:
        """Return the slide-description markdown (or ``""`` — R9).

        Each slide is described by a **separate** vision call carrying a single
        image and no transcript. An empty slide set returns ``""`` with **no**
        HTTP call. A valid empty or whitespace-only response for a slide simply
        contributes nothing. Exceeding the deterministic ceiling (R23) — either
        the slide-count or the whole-deck encoded-byte bound — raises
        ``SlideDescribeError`` **before** any HTTP call. Transport/format
        failures raise ``SlideDescribeError``.

        ``transcript_text`` is accepted to satisfy the
        :class:`~transcriber.backends.interfaces.SlidesBackend` protocol and the
        orchestrator call site, but is intentionally **unused**: the descriptor
        is image-only.
        """
        del transcript_text  # image-only descriptor; intentionally unused.

        slides = list(slides)
        if not slides:
            logger.info("describe_slides[openrouter]: no slides, returning empty")
            return ""

        if config.openrouter is None:
            raise SlideDescribeError(
                "openrouter config section is required for the openrouter "
                "slides backend but is not configured"
            )

        model = config.openrouter.slides_model

        # Slide-count ceiling check (R23), once, before any network call.
        max_slides = getattr(config.openrouter, "max_slides", MAX_SLIDES)
        if len(slides) > max_slides:
            raise SlideDescribeError(
                f"slide deck exceeds the maximum of {max_slides} slides "
                f"({len(slides)} provided); raise the ceiling "
                "(openrouter.max_slides), split the recording, or disable the "
                "slides stage for this host"
            )

        # Whole-deck encoded-byte ceiling (R23): encode every slide once and
        # enforce the TOTAL BEFORE any HTTP call, so an over-ceiling deck
        # hard-fails before a single image is sent. The cache is reused per
        # slide so images are not re-encoded.
        encoded_cache = _preflight_deck_encoded_bytes(config, slides)

        started = time.monotonic()
        parts: list[str] = []
        n_slides = len(slides)
        for i, slide in enumerate(slides):
            messages, encoded_bytes = _build_single_slide_message(
                config, slide, encoded_cache=encoded_cache
            )
            logger.info(
                "describe_slides[openrouter]: model=%s slide=%d/%d "
                "encoded_bytes=%d",
                model,
                i + 1,
                n_slides,
                encoded_bytes,
            )
            content = _openrouter_vision(config, messages, model, timeout=timeout)
            if content.strip():
                parts.append(content.strip())

        elapsed = time.monotonic() - started
        logger.info(
            "describe_slides[openrouter]: model=%s slides=%d elapsed=%.2fs",
            model,
            n_slides,
            elapsed,
        )

        # A valid empty/whitespace response for every slide is not an error.
        return "\n\n".join(parts)
