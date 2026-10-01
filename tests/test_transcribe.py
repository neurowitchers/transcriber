"""Tests for transcriber.backends.transcribe_openrouter.

No real network, no real audio:

* ``httpx.post`` is monkeypatched at the module seam.
* chunk/stitch/reconcile are exercised with synthetic ``TranscriptChunk``
  fixtures and a monkeypatched ``_run_command``.
"""

from __future__ import annotations

import base64
import logging
from pathlib import Path

import pytest

import transcriber.backends.transcribe_openrouter as tx
from transcriber.backends.errors import TranscribeError
from transcriber.config import (
    Agent,
    Config,
    Notion,
    OpenRouter,
    SlidesStage,
    Stages,
    Summary,
    Telegram,
    Timeouts,
    Transcribe,
)

API_KEY_ENV = "OPENROUTER_API_KEY"
MODEL = "microsoft/mai-transcribe-2"


# --------------------------------------------------------------------------- #
# Fixtures / helpers
# --------------------------------------------------------------------------- #
def make_config(
    *,
    with_openrouter: bool = True,
    recordings_dir: str = "./recordings",
    diarize: bool = True,
    segment_seconds: int = 480,
    overlap_seconds: int = 5,
    model_id: str = MODEL,
    language: str = "en",
) -> Config:
    return Config(
        recordings_dir=recordings_dir,
        stages=Stages(
            slides=SlidesStage(enabled=False, backend="openrouter"), s3_sync=False
        ),
        transcribe=Transcribe(
            model_id=model_id,
            diarize=diarize,
            segment_seconds=segment_seconds,
            overlap_seconds=overlap_seconds,
        ),
        summary=Summary(language=language, sections=["overview"]),
        agent=Agent(cli="agy", extra_args=[], output_file="{basename}.md"),
        notion=Notion(server="n", parent_page_id="p", insert="subpage"),
        telegram=Telegram(
            bot_token_env="TELEGRAM_BOT_TOKEN", default_chat_id="1", routing={}
        ),
        timeouts=Timeouts(),
        openrouter=(
            OpenRouter(api_key_env=API_KEY_ENV) if with_openrouter else None
        ),
    )


class FakeResponse:
    def __init__(self, status_code: int, payload=None, text: str = "") -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = text if text else (str(payload) if payload is not None else "")

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


def fake_poster(responses: list[FakeResponse]):
    """Return a fake ``httpx.post`` yielding the given responses in order and
    recording each call's kwargs."""
    calls: list[dict] = []
    seq = list(responses)

    def _post(url, *, headers=None, json=None, timeout=None):
        calls.append(
            {"url": url, "headers": headers, "json": json, "timeout": timeout}
        )
        return seq.pop(0)

    _post.calls = calls  # type: ignore[attr-defined]
    return _post


@pytest.fixture(autouse=True)
def _set_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(API_KEY_ENV, "sekret-key-value")


@pytest.fixture
def audio_file(tmp_path: Path) -> Path:
    p = tmp_path / "part_000.mp3"
    p.write_bytes(b"RAW-AUDIO-BYTES")
    return p


# --------------------------------------------------------------------------- #
# Task 2 — HTTP seam
# --------------------------------------------------------------------------- #
def test_200_with_segments_parses_speakers(
    monkeypatch: pytest.MonkeyPatch, audio_file: Path
) -> None:
    payload = {
        "text": "hello there general",
        "segments": [
            {"speaker": "0", "start": 0.0, "end": 1.0, "text": "hello"},
            {"speaker": "1", "start": 1.0, "end": 2.0, "text": "there general"},
        ],
        "usage": {"cost": 0.0005},
    }
    post = fake_poster([FakeResponse(200, payload)])
    monkeypatch.setattr(tx.httpx, "post", post)

    chunk = tx._openrouter_transcribe(
        make_config(), audio_file, model=MODEL, diarize=True, language="en", timeout=30
    )
    assert [s.speaker for s in chunk.segments] == ["0", "1"]
    assert chunk.segments[1].text == "there general"
    assert chunk.cost == 0.0005


def test_200_with_only_text_flat_fallback_and_warning(
    monkeypatch: pytest.MonkeyPatch,
    audio_file: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    post = fake_poster([FakeResponse(200, {"text": "just flat text"})])
    monkeypatch.setattr(tx.httpx, "post", post)

    with caplog.at_level(logging.WARNING, logger=tx.logger.name):
        chunk = tx._openrouter_transcribe(
            make_config(), audio_file, model=MODEL, diarize=True, language=None, timeout=30
        )
    assert chunk.segments == []
    assert chunk.raw_text == "just flat text"
    combined = "\n".join(r.getMessage() for r in caplog.records)
    assert "flat text" in combined


def test_diarize_false_omits_provider_block(
    monkeypatch: pytest.MonkeyPatch, audio_file: Path
) -> None:
    post = fake_poster([FakeResponse(200, {"text": "flat"})])
    monkeypatch.setattr(tx.httpx, "post", post)

    chunk = tx._openrouter_transcribe(
        make_config(diarize=False),
        audio_file,
        model=MODEL,
        diarize=False,
        language="en",
        timeout=30,
    )
    body = post.calls[0]["json"]
    assert "provider" not in body
    assert "response_format" not in body
    assert chunk.segments == []  # flat → no Speaker prefixes downstream


def test_diarize_true_sends_provider_toggle_and_verbose_json(
    monkeypatch: pytest.MonkeyPatch, audio_file: Path
) -> None:
    post = fake_poster([FakeResponse(200, {"text": "x", "segments": []})])
    monkeypatch.setattr(tx.httpx, "post", post)

    tx._openrouter_transcribe(
        make_config(), audio_file, model=MODEL, diarize=True, language="en", timeout=30
    )
    body = post.calls[0]["json"]
    assert body["response_format"] == "verbose_json"
    assert body["provider"]["options"]["azure"]["diarization"]["enabled"] is True
    # Raw base64 (not a data: URI).
    assert body["input_audio"]["data"] == base64.b64encode(
        b"RAW-AUDIO-BYTES"
    ).decode("ascii")
    assert not body["input_audio"]["data"].startswith("data:")
    assert body["input_audio"]["format"] == "mp3"


def test_400_raises_transcribe_error_no_retry(
    monkeypatch: pytest.MonkeyPatch, audio_file: Path
) -> None:
    post = fake_poster(
        [FakeResponse(400, text="model cannot diarize"), FakeResponse(200, {"text": "x"})]
    )
    monkeypatch.setattr(tx.httpx, "post", post)

    with pytest.raises(TranscribeError) as exc:
        tx._openrouter_transcribe(
            make_config(), audio_file, model=MODEL, diarize=True, language="en", timeout=30
        )
    assert "400" in str(exc.value)
    assert len(post.calls) == 1  # NO retry on 400


def test_429_then_200_retries_then_succeeds(
    monkeypatch: pytest.MonkeyPatch, audio_file: Path
) -> None:
    monkeypatch.setattr(tx.time, "sleep", lambda _s: None)
    post = fake_poster(
        [FakeResponse(429, text="rate limited"), FakeResponse(200, {"text": "ok"})]
    )
    monkeypatch.setattr(tx.httpx, "post", post)

    chunk = tx._openrouter_transcribe(
        make_config(), audio_file, model=MODEL, diarize=True, language="en", timeout=30
    )
    assert chunk.raw_text == "ok"
    assert len(post.calls) == 2


def test_key_and_base64_never_logged_or_in_exception(
    monkeypatch: pytest.MonkeyPatch,
    audio_file: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(tx.time, "sleep", lambda _s: None)
    # All attempts 500 → raises after MAX_ATTEMPTS; body echoes nothing secret.
    post = fake_poster([FakeResponse(500, text="server boom")] * tx.MAX_ATTEMPTS)
    monkeypatch.setattr(tx.httpx, "post", post)

    b64 = base64.b64encode(b"RAW-AUDIO-BYTES").decode("ascii")
    with caplog.at_level(logging.DEBUG, logger=tx.logger.name):
        with pytest.raises(TranscribeError) as exc:
            tx._openrouter_transcribe(
                make_config(), audio_file, model=MODEL, diarize=True, language="en", timeout=1
            )
    combined = "\n".join(r.getMessage() for r in caplog.records)
    assert "sekret-key-value" not in combined
    assert b64 not in combined
    assert "sekret-key-value" not in str(exc.value)
    assert b64 not in str(exc.value)


def test_missing_openrouter_config_raises(
    monkeypatch: pytest.MonkeyPatch, audio_file: Path
) -> None:
    with pytest.raises(TranscribeError):
        tx._openrouter_transcribe(
            make_config(with_openrouter=False),
            audio_file,
            model=MODEL,
            diarize=True,
            language="en",
            timeout=30,
        )


def test_words_fallback_when_no_segments(
    monkeypatch: pytest.MonkeyPatch, audio_file: Path
) -> None:
    payload = {
        "text": "hi there",
        "words": [
            {"word": "hi", "speaker": "0", "start": 0.0, "end": 0.5},
            {"word": "there", "speaker": "0", "start": 0.5, "end": 1.0},
            {"word": "yo", "speaker": "1", "start": 1.0, "end": 1.5},
        ],
    }
    post = fake_poster([FakeResponse(200, payload)])
    monkeypatch.setattr(tx.httpx, "post", post)
    chunk = tx._openrouter_transcribe(
        make_config(), audio_file, model=MODEL, diarize=True, language="en", timeout=30
    )
    assert [s.speaker for s in chunk.segments] == ["0", "1"]
    assert chunk.segments[0].text == "hi there"


# --------------------------------------------------------------------------- #
# Task 3 — chunk_audio
# --------------------------------------------------------------------------- #
def test_chunk_audio_per_part_ss_t_argv(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import transcriber.pipeline as pipeline_mod

    calls: list[list[str]] = []
    monkeypatch.setattr(
        pipeline_mod, "_run_command", lambda argv, timeout, stdout_path=None: calls.append(list(argv))
    )

    mp3 = tmp_path / "rec.mp3"
    mp3.write_bytes(b"x")
    work_dir = tmp_path / "transcribe_work.rec"

    parts = tx.chunk_audio(
        mp3, work_dir, segment_seconds=480, overlap_seconds=5, timeout=30, duration_seconds=1000.0
    )
    # ceil(1000/480) = 3 parts.
    assert len(parts) == 3
    assert parts[0] == work_dir / "part_000.mp3"
    assert parts[2] == work_dir / "part_002.mp3"

    # Per-part explicit -ss/-t, -c copy; NOT a -f segment call.
    assert "-f" not in calls[0]
    assert calls[0][:4] == ["ffmpeg", "-hide_banner", "-loglevel", "error"]
    assert "-ss" in calls[0] and "-t" in calls[0]
    assert calls[0][calls[0].index("-ss") + 1] == "0"
    assert calls[1][calls[1].index("-ss") + 1] == "480"
    assert calls[0][calls[0].index("-t") + 1] == "485"
    assert "-c" in calls[0] and calls[0][calls[0].index("-c") + 1] == "copy"
    # The transcribe_work.<name> path, not extracted_slides.
    assert "transcribe_work.rec" in str(parts[0])


def test_chunk_audio_single_part_when_duration_unknown(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import transcriber.pipeline as pipeline_mod

    calls: list[list[str]] = []
    monkeypatch.setattr(
        pipeline_mod, "_run_command", lambda argv, timeout, stdout_path=None: calls.append(list(argv))
    )
    mp3 = tmp_path / "rec.mp3"
    mp3.write_bytes(b"x")
    parts = tx.chunk_audio(
        mp3, tmp_path / "transcribe_work.rec", 480, 5, 30, duration_seconds=None
    )
    assert len(parts) == 1
    assert len(calls) == 1


# --------------------------------------------------------------------------- #
# Task 4 — _probe_duration_seconds (production duration source, BUG-1 regression)
# --------------------------------------------------------------------------- #
def test_probe_duration_parses_ffprobe_output(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """ffprobe output is parsed into a float duration (the production path)."""
    import transcriber.pipeline as pipeline_mod

    def fake_run(argv, timeout, stdout_path=None):
        assert argv[0] == "ffprobe"
        # ffprobe writes the duration to stdout_path; simulate that.
        Path(stdout_path).write_text("3675.123\n", encoding="utf-8")

    monkeypatch.setattr(pipeline_mod, "_run_command", fake_run)
    mp3 = tmp_path / "rec.mp3"
    mp3.write_bytes(b"x")
    assert tx._probe_duration_seconds(mp3) == pytest.approx(3675.123)


def test_probe_duration_none_on_bad_output(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import transcriber.pipeline as pipeline_mod

    def fake_run(argv, timeout, stdout_path=None):
        Path(stdout_path).write_text("N/A\n", encoding="utf-8")

    monkeypatch.setattr(pipeline_mod, "_run_command", fake_run)
    mp3 = tmp_path / "rec.mp3"
    mp3.write_bytes(b"x")
    assert tx._probe_duration_seconds(mp3) is None


def test_probe_duration_none_on_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import transcriber.pipeline as pipeline_mod

    def fake_run(argv, timeout, stdout_path=None):
        raise RuntimeError("ffprobe missing")

    monkeypatch.setattr(pipeline_mod, "_run_command", fake_run)
    mp3 = tmp_path / "rec.mp3"
    mp3.write_bytes(b"x")
    assert tx._probe_duration_seconds(mp3) is None


def test_long_recording_chunks_into_multiple_parts_without_probe_mock(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """End-to-end: a long recording is chunked via the REAL probe path.

    Regression for BUG-1: before the fix, _probe_duration_seconds was a stub
    returning None, so a long recording was cut into a single part and the
    remainder silently dropped. Here we drive the real probe (its ffprobe call
    goes through the monkeypatched _run_command seam) and assert >1 part is cut.
    """
    import transcriber.pipeline as pipeline_mod

    ff_calls: list[list[str]] = []

    def fake_run(argv, timeout, stdout_path=None):
        ff_calls.append(list(argv))
        if argv and argv[0] == "ffprobe":
            Path(stdout_path).write_text("1800.0\n", encoding="utf-8")  # 30 min

    monkeypatch.setattr(pipeline_mod, "_run_command", fake_run)

    def fake_seam(config, part_path, **kwargs):
        return tx.TranscriptChunk(
            index=0, offset=0.0, segments=[], cost=0.0, raw_text="chunk text"
        )

    monkeypatch.setattr(tx, "_openrouter_transcribe", fake_seam)

    cfg = make_config(
        recordings_dir=str(tmp_path),
        diarize=False,
        segment_seconds=480,
        overlap_seconds=5,
    )
    mp3 = tmp_path / "rec.mp3"
    mp3.write_bytes(b"x")
    txt = tmp_path / "rec.txt"
    tx.transcribe_recording(mp3, txt, cfg)

    # ceil(1800/480) = 4 parts → 4 ffmpeg slice calls (plus 1 ffprobe call).
    slice_calls = [c for c in ff_calls if c and c[0] == "ffmpeg"]
    assert len(slice_calls) == 4
    assert txt.read_text(encoding="utf-8").strip() != ""


# --------------------------------------------------------------------------- #
# Task 3 — stitch
# --------------------------------------------------------------------------- #
def _chunk(index: int, segs: list[tuple[str, float, float, str]], raw: str = "", cost: float = 0.0) -> tx.TranscriptChunk:
    return tx.TranscriptChunk(
        index=index,
        offset=0.0,
        segments=[tx.SpeakerSegment(sp, s, e, t) for sp, s, e, t in segs],
        cost=cost,
        raw_text=raw,
    )


def test_stitch_offsets_are_monotonic() -> None:
    chunks = [
        _chunk(0, [("0", 0.0, 10.0, "a"), ("1", 10.0, 20.0, "b")]),
        _chunk(1, [("0", 0.0, 10.0, "c")]),
        _chunk(2, [("1", 0.0, 10.0, "d")]),
    ]
    stitched = tx.stitch(chunks, segment_seconds=480)
    assert stitched[0].offset == 0.0
    assert stitched[1].offset == 480.0
    assert stitched[2].offset == 960.0
    # Segment times shifted by cumulative start → monotonic across chunks.
    starts = [s.start for c in stitched for s in c.segments]
    assert starts == sorted(starts)
    assert stitched[1].segments[0].start == 480.0


# --------------------------------------------------------------------------- #
# Task 3 — reconcile_speakers
# --------------------------------------------------------------------------- #
def test_reconcile_permuted_labels_yield_consistent_global_speakers() -> None:
    # Chunk 0: speaker A="0", B="1", both active in the overlap tail. Chunk 1
    # overlaps and PERMUTES labels (provider calls A="1", B="0"). Overlap-window
    # alignment by time must remap them back to consistent global ids.
    seg = 480
    ov = 10
    chunks = [
        _chunk(
            0,
            [
                ("0", 0.0, 472.0, "alpha speaks long"),
                # Both speakers active in the overlap tail [480, 490):
                ("0", 482.0, 484.0, "alpha overlap tail"),
                ("1", 486.0, 489.0, "beta overlap tail"),
            ],
        ),
        # chunk 1 local times; stitch offsets by 480. Overlap window [480, 490).
        # Local "1" (alpha) sits at global 482-484; local "0" (beta) at 486-489
        # — labels permuted vs chunk 0. Time alignment must remap them.
        _chunk(
            1,
            [
                ("1", 2.0, 4.0, "alpha overlap tail"),
                ("0", 6.0, 9.0, "beta overlap tail"),
                ("1", 12.0, 20.0, "alpha continues"),
            ],
        ),
    ]
    stitched = tx.stitch(chunks, seg)
    reconciled = tx.reconcile_speakers(stitched, seg, ov)

    # Beta's utterances must share one global id; alpha another.
    beta_ids = {s.speaker for s in reconciled if "beta" in s.text}
    alpha_ids = {s.speaker for s in reconciled if "alpha" in s.text}
    assert len(beta_ids) == 1
    assert len(alpha_ids) == 1
    assert beta_ids != alpha_ids


def test_reconcile_unmatched_speaker_gets_new_global_id_and_warns(
    caplog: pytest.LogCaptureFixture,
) -> None:
    seg = 480
    ov = 5
    chunks = [
        _chunk(0, [("0", 0.0, 482.0, "speaker zero all through overlap")]),
        # chunk 1: a brand-new speaker "9" appears AFTER the overlap window
        # (silent during overlap) → must get a new global id + warning.
        _chunk(
            1,
            [
                ("0", 0.0, 2.0, "speaker zero all through overlap"),
                ("9", 10.0, 20.0, "a new voice entirely"),
            ],
        ),
    ]
    stitched = tx.stitch(chunks, seg)
    with caplog.at_level(logging.WARNING, logger=tx.logger.name):
        reconciled = tx.reconcile_speakers(stitched, seg, ov)

    speakers = {s.speaker for s in reconciled}
    # Two distinct global ids: the carried-over "0" and the new voice.
    assert len(speakers) == 2
    new_voice = [s for s in reconciled if "new voice" in s.text][0]
    carried = [s for s in reconciled if "zero" in s.text][0]
    assert new_voice.speaker != carried.speaker
    combined = "\n".join(r.getMessage() for r in caplog.records)
    assert "unmatched" in combined


# --------------------------------------------------------------------------- #
# Task 3 — render_transcript
# --------------------------------------------------------------------------- #
def test_render_speaker_lines_no_timestamps() -> None:
    segs = [
        tx.SpeakerSegment("0", 0.0, 1.0, "hello"),
        tx.SpeakerSegment("0", 1.0, 2.0, "world"),
        tx.SpeakerSegment("1", 2.0, 3.0, "goodbye"),
    ]
    out = tx.render_transcript(segs, [], diarize=True)
    assert out == "Speaker 0: hello world\nSpeaker 1: goodbye"
    # No timestamp markers.
    assert "0.0" not in out and "00:" not in out


def test_render_flat_text_when_diarize_false() -> None:
    chunks = [
        _chunk(0, [], raw="part one text"),
        _chunk(1, [], raw="part two text"),
    ]
    stitched = tx.stitch(chunks, 480)
    out = tx.render_transcript([], stitched, diarize=False)
    assert out == "part one text\npart two text"
    assert "Speaker" not in out


def test_render_flat_text_when_no_labels() -> None:
    chunks = [_chunk(0, [], raw="flat only")]
    out = tx.render_transcript([], chunks, diarize=True)
    assert out == "flat only"
    assert "Speaker" not in out
