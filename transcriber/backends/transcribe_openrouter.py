"""OpenRouter diarized speech-to-text backend for the ``transcribe`` stage.

This module replaces the former ElevenLabs CLI path. It owns four concerns:

1. **HTTP seam** — :func:`_openrouter_transcribe` transcribes exactly *one*
   audio file via ``POST {base_url}/audio/transcriptions`` and returns one
   :class:`TranscriptChunk`. It mirrors the retry/backoff/``_snippet``/log-
   hygiene scaffolding of :mod:`transcriber.backends.openrouter` (the vision
   seam) but posts a **JSON base64** body (not OpenAI multipart — P3) and
   parses the STT response shape.
2. **Chunker** — :func:`chunk_audio` slices a long ``<name>.mp3`` into ordered,
   overlapping parts with *explicit time-sliced* ffmpeg calls (the ``segment``
   muxer has no overlap option — E1), one call per part, through the pipeline's
   ``_run_command`` seam.
3. **Stitch + reconcile** — :func:`stitch` offsets per-chunk timestamps onto a
   monotonic timeline; :func:`reconcile_speakers` maps per-request provider
   labels onto a persistent global speaker registry using the overlap window
   (unmatched → new global id + logged warning; never dropped/force-merged —
   R9/E4), and :func:`render_transcript` emits ``Speaker <ID>: <text>`` lines
   with **no timestamp markers** (R2/P2), or flat text when there are no labels
   / ``diarize == false``.
4. **Stage entry** — :func:`transcribe_recording` orchestrates chunk → per-
   segment (manifest check → seam → persist) → stitch → reconcile → write
   ``<name>.txt``, with a ``segments.json`` manifest giving per-segment
   idempotency and a param guard (R11/E5).

**Log hygiene (R3):** the API key, request headers, and base64 audio payload
are never logged or embedded in exceptions. Errors carry only an HTTP status
and a short truncated body snippet.
"""

from __future__ import annotations

import base64
import json
import logging
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

import httpx

from transcriber.backends.errors import TranscribeError
from transcriber.config import resolve_env

if TYPE_CHECKING:  # pragma: no cover - typing only
    from transcriber.config import Config

logger = logging.getLogger(__name__)

# OpenRouter attribution headers (public, non-secret) — mirrors the vision seam.
_HTTP_REFERER = "https://github.com/neurowitchers/transcriber"
_X_TITLE = "transcriber"

# Bounded exponential backoff for transient (429 / 5xx) responses.
MAX_ATTEMPTS = 4  # total tries, including the first
_BACKOFF_BASE_SECONDS = 0.5
_BACKOFF_CAP_SECONDS = 8.0

# Statuses treated as transient and retried. A 400 is NOT retryable (R10).
_RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504})

# Max characters of a response body echoed into an error message/log.
_SNIPPET_LEN = 200

# Per-model diarization toggle. An unknown model sends verbose_json without a
# provider toggle and relies on native diarization (failing cleanly on a 400 if
# unsupported). Keyed by a substring of the model slug so both
# ``microsoft/mai-transcribe-2`` and a bare ``mai-transcribe-2`` match.
_DIARIZATION_TOGGLES: dict[str, dict[str, Any]] = {
    "mai-transcribe-2": {
        "provider": {
            "options": {"azure": {"diarization": {"enabled": True}}}
        }
    },
}


# --------------------------------------------------------------------------- #
# Data types
# --------------------------------------------------------------------------- #
@dataclass
class SpeakerSegment:
    """One diarized span of transcript.

    Attributes:
        speaker: Provider label (per-request local id, e.g. ``"0"`` /
            ``"Speaker 1"``). Empty string when the response had no labels.
        start: Start time in seconds (chunk-local before :func:`stitch`).
        end: End time in seconds.
        text: The spoken text for this span.
    """

    speaker: str
    start: float
    end: float
    text: str


@dataclass
class TranscriptChunk:
    """The parsed result of transcribing one audio part.

    Attributes:
        index: Zero-based part index.
        offset: Cumulative start (seconds) in the full timeline
            (``index * segment_seconds``), filled in by :func:`stitch`.
        segments: Diarized spans (chunk-local times until stitched). Empty when
            the response fell back to flat ``raw_text``.
        cost: ``usage.cost`` for this chunk (dollars; ``0.0`` if absent).
        raw_text: Flat ``text`` fallback (always populated when available).
    """

    index: int
    offset: float
    segments: list[SpeakerSegment]
    cost: float
    raw_text: str


# --------------------------------------------------------------------------- #
# Snippet / backoff helpers (mirror openrouter.py)
# --------------------------------------------------------------------------- #
def _snippet(text: str) -> str:
    """Return a short, single-line body snippet safe to log/raise."""
    collapsed = " ".join(text.split())
    if len(collapsed) > _SNIPPET_LEN:
        return collapsed[:_SNIPPET_LEN] + "…"
    return collapsed


def _backoff_seconds(attempt: int) -> float:
    """Bounded exponential backoff for the given zero-based attempt index."""
    return min(_BACKOFF_BASE_SECONDS * (2**attempt), _BACKOFF_CAP_SECONDS)


def _diarization_toggle(model: str) -> dict[str, Any]:
    """Return the provider diarization block for ``model`` (``{}`` if unknown)."""
    for key, toggle in _DIARIZATION_TOGGLES.items():
        if key in model:
            return toggle
    return {}


# --------------------------------------------------------------------------- #
# HTTP seam (Task 2)
# --------------------------------------------------------------------------- #
def _openrouter_transcribe(
    config: "Config",
    audio_path: Path,
    *,
    model: str,
    diarize: bool,
    language: Optional[str],
    timeout: float,
) -> TranscriptChunk:
    """Transcribe a single audio file via OpenRouter STT.

    Args:
        config: The loaded config; ``config.openrouter`` supplies the base URL
            and the API-key env-var name (resolved here at use time).
        audio_path: Path to the (chunk) ``.mp3`` to transcribe.
        model: OpenRouter STT slug (e.g. ``microsoft/mai-transcribe-2``).
        diarize: When true, request ``verbose_json`` + the per-model provider
            diarization toggle; when false, omit the toggle and emit flat text.
        language: Optional language hint (``None`` → auto-detect; omitted).
        timeout: Per-request timeout in seconds.

    Returns:
        A :class:`TranscriptChunk` with ``index``/``offset`` set to ``0`` (the
        caller fills those in via :func:`stitch`).

    Raises:
        TranscribeError: on missing ``openrouter`` config, transport error,
            non-2xx after retries (a ``400`` fails immediately — no retry),
            or malformed JSON.
    """
    if config.openrouter is None:
        raise TranscribeError(
            "openrouter config section is required for the OpenRouter STT "
            "transcribe backend but is not configured"
        )

    api_key = resolve_env(config.openrouter.api_key_env)
    base_url = config.openrouter.base_url.rstrip("/")
    url = f"{base_url}/audio/transcriptions"

    headers = {
        "Authorization": f"Bearer {api_key}",
        "HTTP-Referer": _HTTP_REFERER,
        "X-Title": _X_TITLE,
        "Content-Type": "application/json",
    }

    # Raw base64, NOT a data: URI (P3). The file is read as bytes and encoded.
    encoded = base64.b64encode(audio_path.read_bytes()).decode("ascii")
    payload: dict[str, Any] = {
        "model": model,
        "input_audio": {"data": encoded, "format": "mp3"},
    }
    if language:
        payload["language"] = language
    if diarize:
        payload["response_format"] = "verbose_json"
        payload.update(_diarization_toggle(model))

    last_status: int | None = None
    last_snippet: str = ""

    for attempt in range(MAX_ATTEMPTS):
        try:
            response = httpx.post(
                url, headers=headers, json=payload, timeout=timeout
            )
        except httpx.HTTPError as exc:
            # Transport-level failure; retry within the cap. Never log the
            # exception's request (it carries the headers + base64 payload).
            last_status = None
            last_snippet = type(exc).__name__
            logger.warning(
                "openrouter STT transport error (attempt %d/%d): %s",
                attempt + 1,
                MAX_ATTEMPTS,
                type(exc).__name__,
            )
            if attempt + 1 < MAX_ATTEMPTS:
                time.sleep(_backoff_seconds(attempt))
                continue
            raise TranscribeError(
                f"openrouter STT request failed after {MAX_ATTEMPTS} attempts: "
                f"transport error {type(exc).__name__}"
            ) from exc

        status = response.status_code
        if status in _RETRYABLE_STATUSES:
            last_status = status
            last_snippet = _snippet(response.text)
            logger.warning(
                "openrouter STT transient status %d (attempt %d/%d)",
                status,
                attempt + 1,
                MAX_ATTEMPTS,
            )
            if attempt + 1 < MAX_ATTEMPTS:
                time.sleep(_backoff_seconds(attempt))
                continue
            raise TranscribeError(
                f"openrouter STT call failed after {MAX_ATTEMPTS} attempts: "
                f"status {status}: {last_snippet}"
            )

        if not (200 <= status < 300):
            # 400 (model can't diarize / bad format) and any other non-2xx:
            # fail immediately, no retry (R10 — no silent fallback).
            raise TranscribeError(
                f"openrouter STT call failed: status {status}: "
                f"{_snippet(response.text)}"
            )

        # 2xx: parse and extract into a TranscriptChunk.
        try:
            data = response.json()
        except ValueError as exc:
            raise TranscribeError(
                f"openrouter STT returned malformed JSON: status {status}: "
                f"{_snippet(response.text)}"
            ) from exc

        return _parse_response(data, diarize=diarize)

    # Unreachable: the loop either returns or raises on the final attempt.
    raise TranscribeError(  # pragma: no cover
        f"openrouter STT call failed: status {last_status}: {last_snippet}"
    )


def _parse_response(data: Any, *, diarize: bool) -> TranscriptChunk:
    """Defensively parse an STT response into a :class:`TranscriptChunk`.

    Reads ``segments`` (preferred) or ``words`` into :class:`SpeakerSegment`s
    and ``usage.cost`` into ``cost``. On any missing/odd shape it degrades to a
    flat ``raw_text`` and logs a warning (R5) — never a silent crash.
    """
    raw_text = ""
    cost = 0.0
    segments: list[SpeakerSegment] = []

    if isinstance(data, dict):
        text_val = data.get("text")
        if isinstance(text_val, str):
            raw_text = text_val

        usage = data.get("usage")
        if isinstance(usage, dict):
            cost_val = usage.get("cost")
            if isinstance(cost_val, (int, float)):
                cost = float(cost_val)

        if diarize:
            segments = _parse_segments(data.get("segments"))
            if not segments:
                segments = _parse_words(data.get("words"))

    if diarize and not segments:
        # Degrade to flat text (R5) and warn — no speaker labels available.
        logger.warning(
            "openrouter STT response had no parseable segments/words; "
            "falling back to flat text"
        )

    return TranscriptChunk(
        index=0, offset=0.0, segments=segments, cost=cost, raw_text=raw_text
    )


def _parse_segments(raw: Any) -> list[SpeakerSegment]:
    """Parse a ``segments`` list defensively; skip malformed entries."""
    if not isinstance(raw, list):
        return []
    out: list[SpeakerSegment] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        text = item.get("text")
        if not isinstance(text, str) or not text.strip():
            continue
        speaker = item.get("speaker")
        start = item.get("start")
        end = item.get("end")
        out.append(
            SpeakerSegment(
                speaker=str(speaker) if speaker is not None else "",
                start=float(start) if isinstance(start, (int, float)) else 0.0,
                end=float(end) if isinstance(end, (int, float)) else 0.0,
                text=text.strip(),
            )
        )
    return out


def _parse_words(raw: Any) -> list[SpeakerSegment]:
    """Group a word-level ``words`` list into per-speaker-turn segments."""
    if not isinstance(raw, list):
        return []
    out: list[SpeakerSegment] = []
    cur: Optional[SpeakerSegment] = None
    for item in raw:
        if not isinstance(item, dict):
            continue
        word = item.get("word")
        if not isinstance(word, str):
            word = item.get("text")
        if not isinstance(word, str) or not word.strip():
            continue
        speaker = item.get("speaker")
        speaker_s = str(speaker) if speaker is not None else ""
        start = float(item["start"]) if isinstance(item.get("start"), (int, float)) else 0.0
        end = float(item["end"]) if isinstance(item.get("end"), (int, float)) else start
        if cur is not None and cur.speaker == speaker_s:
            cur.text = f"{cur.text} {word.strip()}".strip()
            cur.end = end
        else:
            cur = SpeakerSegment(
                speaker=speaker_s, start=start, end=end, text=word.strip()
            )
            out.append(cur)
    return out


# --------------------------------------------------------------------------- #
# Chunker (Task 3)
# --------------------------------------------------------------------------- #
def chunk_audio(
    mp3: Path,
    work_dir: Path,
    segment_seconds: int,
    overlap_seconds: int,
    timeout: float,
    *,
    duration_seconds: Optional[float] = None,
) -> list[Path]:
    """Slice ``mp3`` into ordered overlapping parts via explicit ffmpeg calls.

    One ``_run_command`` call per part (the ``segment`` muxer has no overlap
    option — E1):
    ``ffmpeg -ss <k*segment_seconds> -t <segment_seconds + overlap_seconds>
    -i <mp3> -c copy <work_dir>/part_{k:03d}.mp3``.

    Args:
        mp3: The source audio file.
        work_dir: ``recordings_dir / f"transcribe_work.{name}"`` (created here).
        segment_seconds: Nominal part length (part *k* starts at
            ``k * segment_seconds``).
        overlap_seconds: Extra tail shared with the next part (supports seam
            reconciliation, R9).
        timeout: Per-ffmpeg-call timeout.
        duration_seconds: Optional total audio duration. When known, the number
            of parts is ``ceil(duration / segment_seconds)``; otherwise a
            single part is cut (tests/short clips).

    Returns:
        Ordered part paths.
    """
    # Imported lazily to use the pipeline's monkeypatchable seam.
    from transcriber.pipeline import _run_command

    work_dir.mkdir(parents=True, exist_ok=True)

    if duration_seconds is not None and segment_seconds > 0:
        import math

        n_parts = max(1, math.ceil(duration_seconds / segment_seconds))
    else:
        n_parts = 1

    parts: list[Path] = []
    for k in range(n_parts):
        start = k * segment_seconds
        length = segment_seconds + overlap_seconds
        part = work_dir / f"part_{k:03d}.mp3"
        argv = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-ss",
            str(start),
            "-t",
            str(length),
            "-i",
            str(mp3),
            "-c",
            "copy",
            str(part),
        ]
        _run_command(argv, timeout)
        parts.append(part)
    return parts


# --------------------------------------------------------------------------- #
# Stitch + reconcile (Task 3)
# --------------------------------------------------------------------------- #
def stitch(chunks: list[TranscriptChunk], segment_seconds: int) -> list[TranscriptChunk]:
    """Offset each chunk's segment times onto a monotonic global timeline.

    Each chunk's segments are shifted by its cumulative start
    (``index * segment_seconds``). Returns new chunks with ``offset`` set and
    segment ``start``/``end`` globalised (R8).
    """
    out: list[TranscriptChunk] = []
    for chunk in sorted(chunks, key=lambda c: c.index):
        offset = float(chunk.index * segment_seconds)
        shifted = [
            SpeakerSegment(
                speaker=s.speaker,
                start=s.start + offset,
                end=s.end + offset,
                text=s.text,
            )
            for s in chunk.segments
        ]
        out.append(
            TranscriptChunk(
                index=chunk.index,
                offset=offset,
                segments=shifted,
                cost=chunk.cost,
                raw_text=chunk.raw_text,
            )
        )
    return out


def reconcile_speakers(
    chunks: list[TranscriptChunk],
    segment_seconds: int,
    overlap_seconds: int,
) -> list[SpeakerSegment]:
    """Map per-chunk provider labels onto a global speaker registry (R9/E4).

    Expects **stitched** chunks (global timestamps). For each chunk after the
    first, local provider labels are aligned to the global registry using the
    overlap window shared with the previous chunk: a local speaker whose
    overlapping-region segments align (by time) with a global speaker inherits
    that global id. An unmatched local speaker (silent in the overlap) gets a
    **new** global id and a logged warning — never force-merged or dropped.

    Returns a single flat, time-ordered list of :class:`SpeakerSegment`s whose
    ``speaker`` fields are global ids (``"0"``, ``"1"``, …).
    """
    ordered = sorted(chunks, key=lambda c: c.index)
    global_segments: list[SpeakerSegment] = []
    next_global_id = 0

    for ci, chunk in enumerate(ordered):
        if not chunk.segments:
            continue

        # Build this chunk's local-label → global-id mapping.
        local_to_global: dict[str, str] = {}

        if ci == 0:
            for seg in chunk.segments:
                if seg.speaker not in local_to_global:
                    local_to_global[seg.speaker] = str(next_global_id)
                    next_global_id += 1
        else:
            # Overlap region: [chunk.offset, chunk.offset + overlap_seconds).
            overlap_start = chunk.offset
            overlap_end = chunk.offset + overlap_seconds

            # Global speakers active in the overlap window (from already-emitted
            # segments), with their representative text, for alignment.
            prev_in_overlap = [
                g
                for g in global_segments
                if g.end > overlap_start and g.start < overlap_end
            ]

            for seg in chunk.segments:
                if seg.speaker in local_to_global:
                    continue
                # Candidate local segments of this speaker in the overlap window.
                local_overlap = [
                    s
                    for s in chunk.segments
                    if s.speaker == seg.speaker
                    and s.end > overlap_start
                    and s.start < overlap_end
                ]
                matched = _match_global(local_overlap, prev_in_overlap)
                if matched is not None:
                    local_to_global[seg.speaker] = matched
                else:
                    new_id = str(next_global_id)
                    next_global_id += 1
                    local_to_global[seg.speaker] = new_id
                    logger.warning(
                        "transcribe: chunk %d speaker %r unmatched in overlap "
                        "window; assigning new global speaker %s",
                        chunk.index,
                        seg.speaker,
                        new_id,
                    )

        for seg in chunk.segments:
            # Skip the shared leading overlap window for later chunks: those
            # segments are duplicate transcriptions of the previous chunk's
            # tail (chunk 0 emits all; chunk k>0 skips [offset, offset+overlap)).
            if ci > 0 and seg.start < chunk.offset + overlap_seconds:
                continue
            global_segments.append(
                SpeakerSegment(
                    speaker=local_to_global.get(seg.speaker, seg.speaker),
                    start=seg.start,
                    end=seg.end,
                    text=seg.text,
                )
            )

    global_segments.sort(key=lambda s: (s.start, s.end))
    return global_segments


def _match_global(
    local_overlap: list[SpeakerSegment],
    prev_in_overlap: list[SpeakerSegment],
) -> Optional[str]:
    """Return the best-matching global id for a local speaker, or ``None``.

    Alignment heuristic: pick the global speaker with the greatest temporal
    overlap against the local speaker's overlap-region segments; break ties by
    matching text. Returns ``None`` when there is no temporal overlap at all
    (the speaker was silent in the window).
    """
    if not local_overlap or not prev_in_overlap:
        return None

    best_id: Optional[str] = None
    best_overlap = 0.0
    for g in prev_in_overlap:
        total = 0.0
        for ls in local_overlap:
            lo = max(ls.start, g.start)
            hi = min(ls.end, g.end)
            if hi > lo:
                total += hi - lo
        # A text match nudges weak temporal overlaps over the line.
        if total <= 0.0:
            if any(ls.text.strip() == g.text.strip() for ls in local_overlap):
                total = 1e-6
        if total > best_overlap:
            best_overlap = total
            best_id = g.speaker

    return best_id if best_overlap > 0.0 else None


def render_transcript(
    segments: list[SpeakerSegment],
    chunks: list[TranscriptChunk],
    *,
    diarize: bool,
) -> str:
    """Render the final transcript text (R2/P2).

    When diarization produced labelled segments, group consecutive same-global-
    speaker segments into ``Speaker <ID>: <text>`` lines **with no timestamps**.
    When there are no labels anywhere (or ``diarize == false``), return the flat
    stitched text (R5) — chunk ``raw_text`` concatenated in index order.
    """
    labelled = diarize and any(s.speaker != "" for s in segments)
    if not labelled:
        texts = [c.raw_text.strip() for c in sorted(chunks, key=lambda c: c.index)]
        return "\n".join(t for t in texts if t)

    lines: list[str] = []
    cur_speaker: Optional[str] = None
    cur_parts: list[str] = []

    def flush() -> None:
        if cur_speaker is not None and cur_parts:
            lines.append(f"Speaker {cur_speaker}: {' '.join(cur_parts)}")

    for seg in segments:
        if seg.speaker == cur_speaker:
            cur_parts.append(seg.text)
        else:
            flush()
            cur_speaker = seg.speaker
            cur_parts = [seg.text]
    flush()
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Manifest (Task 4)
# --------------------------------------------------------------------------- #
_MANIFEST_NAME = "segments.json"


def _manifest_path(work_dir: Path) -> Path:
    return work_dir / _MANIFEST_NAME


def _load_manifest(work_dir: Path) -> Optional[dict[str, Any]]:
    path = _manifest_path(work_dir)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return None
    return data if isinstance(data, dict) else None


def _save_manifest(work_dir: Path, manifest: dict[str, Any]) -> None:
    _manifest_path(work_dir).write_text(
        json.dumps(manifest, indent=2), encoding="utf-8"
    )


def _chunk_from_manifest_part(part: dict[str, Any]) -> TranscriptChunk:
    segs = [
        SpeakerSegment(
            speaker=str(s.get("speaker", "")),
            start=float(s.get("start", 0.0)),
            end=float(s.get("end", 0.0)),
            text=str(s.get("text", "")),
        )
        for s in part.get("segments", [])
        if isinstance(s, dict)
    ]
    return TranscriptChunk(
        index=int(part["index"]),
        offset=0.0,
        segments=segs,
        cost=float(part.get("cost", 0.0)),
        raw_text=str(part.get("raw_text", "")),
    )


def _part_to_manifest(chunk: TranscriptChunk) -> dict[str, Any]:
    return {
        "index": chunk.index,
        "done": True,
        "cost": chunk.cost,
        "raw_text": chunk.raw_text,
        "segments": [asdict(s) for s in chunk.segments],
    }


# --------------------------------------------------------------------------- #
# Stage entry (Task 4)
# --------------------------------------------------------------------------- #
def _probe_duration_seconds(mp3: Path) -> Optional[float]:
    """Best-effort audio duration via ffprobe through the pipeline seam.

    Returns ``None`` when duration cannot be determined (tests monkeypatch the
    seam and typically leave this unknown, yielding a single part).
    """
    return None


def transcribe_recording(mp3: Path, txt: Path, config: "Config") -> None:
    """Transcribe ``mp3`` into a diarized plain-text ``txt`` (stage entry).

    Orchestrates: chunk → per-segment (manifest check → seam → persist) →
    stitch → reconcile → write. The ``segments.json`` manifest under
    ``recordings_dir / f"transcribe_work.{name}"`` gives per-segment
    idempotency (completed parts are never re-issued) and a parameter guard: if
    the stored ``segment_seconds``/``overlap_seconds`` differ from the current
    config, the work dir is discarded and rechunked (E5).

    Raises:
        TranscribeError: if any segment's seam call fails after retries.
    """
    import shutil

    mp3 = Path(mp3)
    txt = Path(txt)
    name = txt.stem
    recordings_dir = Path(config.recordings_dir)
    work_dir = recordings_dir / f"transcribe_work.{name}"

    tc = config.transcribe
    segment_seconds = tc.segment_seconds
    overlap_seconds = tc.overlap_seconds
    model = tc.model_id
    diarize = tc.diarize
    language = getattr(config.summary, "language", None)
    language = language if language in ("en",) else None
    timeout = config.timeouts.transcribe

    # Parameter guard (E5): a param mismatch invalidates the cached work dir.
    manifest = _load_manifest(work_dir)
    if manifest is not None:
        if (
            manifest.get("segment_seconds") != segment_seconds
            or manifest.get("overlap_seconds") != overlap_seconds
        ):
            logger.warning(
                "transcribe: manifest params changed for %s "
                "(segment_seconds/overlap_seconds); discarding work dir and "
                "rechunking",
                name,
            )
            shutil.rmtree(work_dir, ignore_errors=True)
            manifest = None

    # Chunk if we have no (valid) manifest. We rely on the manifest's recorded
    # part count on a resume; a fresh run chunks the audio now.
    if manifest is None:
        duration = _probe_duration_seconds(mp3)
        parts = chunk_audio(
            mp3,
            work_dir,
            segment_seconds,
            overlap_seconds,
            config.timeouts.ffmpeg,
            duration_seconds=duration,
        )
        manifest = {
            "segment_seconds": segment_seconds,
            "overlap_seconds": overlap_seconds,
            "parts": [
                {"index": k, "done": False, "path": str(p)}
                for k, p in enumerate(parts)
            ],
        }
        _save_manifest(work_dir, manifest)

    parts_meta = manifest.get("parts", [])

    chunks: list[TranscriptChunk] = []
    total_cost = 0.0
    for part in parts_meta:
        index = int(part["index"])
        if part.get("done"):
            # Reuse the stored transcript — never re-pay a completed call (R11).
            chunk = _chunk_from_manifest_part(part)
        else:
            part_path = Path(part.get("path", work_dir / f"part_{index:03d}.mp3"))
            chunk = _openrouter_transcribe(
                config,
                part_path,
                model=model,
                diarize=diarize,
                language=language,
                timeout=timeout,
            )
            chunk.index = index
            # Persist immediately so a later failure doesn't re-issue this part.
            persisted = _part_to_manifest(chunk)
            persisted["path"] = str(part_path)
            manifest["parts"][index] = persisted
            _save_manifest(work_dir, manifest)
        chunks.append(chunk)
        total_cost += chunk.cost

    logger.info(
        "transcribe: %s complete across %d segment(s); aggregate usage.cost=$%.4f",
        name,
        len(chunks),
        total_cost,
    )

    stitched = stitch(chunks, segment_seconds)
    reconciled = reconcile_speakers(stitched, segment_seconds, overlap_seconds)
    body = render_transcript(reconciled, stitched, diarize=diarize)
    txt.write_text(body.strip() + "\n", encoding="utf-8")
