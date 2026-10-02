"""Tests for transcriber.pipeline.

External binaries (ffmpeg / scenedetect) are never invoked: the child-process
seam :func:`transcriber.pipeline._run_command` is monkeypatched to record calls.
The OpenRouter STT seam (``_openrouter_transcribe``) is monkeypatched too so the
transcribe stage runs without network. A separate test drives the real
``_run_command`` with ``duct`` mocked to verify the expression it builds.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import pytest

import transcriber.backends.transcribe_openrouter as tx
from transcriber import pipeline
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


# --------------------------------------------------------------------------- #
# Config factory
# --------------------------------------------------------------------------- #
def make_config(
    recordings_dir: Path,
    *,
    slides: bool = False,
    model_id: str = "microsoft/mai-transcribe-2",
    diarize: bool = True,
    segment_seconds: int = 480,
    overlap_seconds: int = 5,
    timeouts: Optional[Timeouts] = None,
) -> Config:
    return Config(
        recordings_dir=str(recordings_dir),
        stages=Stages(slides=SlidesStage(enabled=slides), s3_sync=False),
        transcribe=Transcribe(
            model_id=model_id,
            diarize=diarize,
            segment_seconds=segment_seconds,
            overlap_seconds=overlap_seconds,
        ),
        summary=Summary(language="en", sections=["overview"]),
        agent=Agent(cli="agy", extra_args=[], output_file="{basename}.md"),
        notion=Notion(server="n", parent_page_id="p", insert="subpage"),
        telegram=Telegram(bot_token_env="TELEGRAM_BOT_TOKEN", default_chat_id="1", routing={}),
        timeouts=timeouts or Timeouts(ffmpeg=11, scenedetect=22, transcribe=33),
        s3=None,
        openrouter=OpenRouter(api_key_env="OPENROUTER_API_KEY"),
    )


# --------------------------------------------------------------------------- #
# _run_command recorder (records argv/timeout/stdout_path, spawns nothing)
# --------------------------------------------------------------------------- #
@dataclass
class Call:
    argv: list[str]
    timeout: float
    stdout_path: Optional[Path]


@dataclass
class Recorder:
    calls: list[Call] = field(default_factory=list)

    def __call__(
        self,
        argv: list[str],
        timeout: float,
        stdout_path: Optional[Path] = None,
    ) -> None:
        self.calls.append(
            Call(argv=list(argv), timeout=timeout, stdout_path=stdout_path)
        )
        # ffmpeg chunk parts are "created" so downstream file reads succeed.
        if argv and argv[0] == "ffmpeg" and argv[-1].endswith(".mp3"):
            out = Path(argv[-1])
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(b"PART")


@pytest.fixture
def recorder(monkeypatch: pytest.MonkeyPatch) -> Recorder:
    rec = Recorder()
    monkeypatch.setattr(pipeline, "_run_command", rec)
    # transcribe_openrouter imports _run_command lazily from pipeline, so the
    # monkeypatch above covers chunk_audio's ffmpeg calls automatically.
    return rec


@dataclass
class SeamRecorder:
    """Records every ``_openrouter_transcribe`` call and returns a canned
    diarized chunk so the transcribe stage produces a speaker-attributed txt."""

    calls: list[Path] = field(default_factory=list)
    fail_indices: set = field(default_factory=set)
    _n: int = 0

    def __call__(self, config, audio_path, *, model, diarize, language, timeout):
        self.calls.append(Path(audio_path))
        idx = self._n
        self._n += 1
        if idx in self.fail_indices:
            from transcriber.backends.errors import TranscribeError

            raise TranscribeError(f"simulated failure on segment {idx}")
        return tx.TranscriptChunk(
            index=0,
            offset=0.0,
            segments=[
                tx.SpeakerSegment("0", 0.0, 1.0, f"hello from part {idx}"),
                tx.SpeakerSegment("1", 1.0, 2.0, f"reply from part {idx}"),
            ],
            cost=0.001,
            raw_text=f"flat {idx}",
        )


@pytest.fixture
def seam(monkeypatch: pytest.MonkeyPatch) -> SeamRecorder:
    rec = SeamRecorder()
    monkeypatch.setattr(tx, "_openrouter_transcribe", rec)
    return rec


def touch(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("x", encoding="utf-8")
    return path


# --------------------------------------------------------------------------- #
# Command-line / argument assertions per toggle
# --------------------------------------------------------------------------- #
def test_ffmpeg_command_line(
    recorder: Recorder, seam: SeamRecorder, tmp_path: Path
) -> None:
    mp4 = touch(tmp_path / "meeting.mp4")
    cfg = make_config(tmp_path)

    result = pipeline.process_recording(mp4, cfg)

    # The audio-EXTRACT ffmpeg call (not chunk_audio's -ss/-t slices).
    ffmpeg_calls = [
        c for c in recorder.calls if c.argv[0] == "ffmpeg" and "-c:a" in c.argv
    ]
    assert len(ffmpeg_calls) == 1
    call = ffmpeg_calls[0]
    assert call.argv == [
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
        str(tmp_path / "meeting.mp3"),
    ]
    assert call.stdout_path is None
    assert call.timeout == 11
    assert "mp3" in result.new_artifacts


def test_transcribe_delegates_to_openrouter_and_writes_diarized_txt(
    recorder: Recorder, seam: SeamRecorder, tmp_path: Path
) -> None:
    mp4 = touch(tmp_path / "talk.mp4")
    # Pre-create the mp3 so the transcribe stage runs (skip ffmpeg extract).
    touch(tmp_path / "talk.mp3")
    cfg = make_config(tmp_path, model_id="microsoft/mai-transcribe-2")

    result = pipeline.process_recording(mp4, cfg)

    # No elevenlabs invocation anywhere.
    assert not any(c.argv[0] == "elevenlabs" for c in recorder.calls)
    # The seam was called once (single part for a short/unknown-duration clip).
    assert len(seam.calls) == 1
    # chunk_audio cut the part via ffmpeg -ss/-t -c copy.
    chunk_calls = [
        c
        for c in recorder.calls
        if c.argv[0] == "ffmpeg" and "-ss" in c.argv and "-c" in c.argv
    ]
    assert len(chunk_calls) == 1
    assert "txt" in result.new_artifacts
    # The .txt is speaker-attributed (Speaker <ID>:) with no timestamps.
    body = (tmp_path / "talk.txt").read_text(encoding="utf-8")
    assert "Speaker 0:" in body
    assert "Speaker 1:" in body
    assert "00:" not in body


def test_transcribe_retry_reissues_only_failed_segment(
    recorder: Recorder, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 2-segment recording: segment 2 fails on the first run, then a rerun
    re-calls the seam ONLY for segment 2 (manifest-gated idempotency)."""
    mp4 = touch(tmp_path / "long.mp4")
    touch(tmp_path / "long.mp3")
    # Force a 2-part chunking by making the duration probe report > 1 segment.
    monkeypatch.setattr(tx, "_probe_duration_seconds", lambda mp3: 700.0)
    cfg = make_config(tmp_path, segment_seconds=480, overlap_seconds=5)

    # First run: part index 1 fails.
    seam1 = SeamRecorder(fail_indices={1})
    monkeypatch.setattr(tx, "_openrouter_transcribe", seam1)
    with pytest.raises(Exception):
        pipeline.process_recording(mp4, cfg)
    assert len(seam1.calls) == 2  # tried both parts; part 1 raised

    # Rerun: a fresh seam that never fails. Only the failed/missing part 1
    # should be re-issued (part 0 is done in the manifest).
    seam2 = SeamRecorder()
    monkeypatch.setattr(tx, "_openrouter_transcribe", seam2)
    result = pipeline.process_recording(mp4, cfg)
    assert len(seam2.calls) == 1  # ONLY segment 2 re-issued
    assert "part_001.mp3" in str(seam2.calls[0])
    assert "txt" in result.new_artifacts
    assert (tmp_path / "long.txt").exists()


def test_transcribe_param_change_invalidates_and_rechunks(
    recorder: Recorder, seam: SeamRecorder, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mp4 = touch(tmp_path / "p.mp4")
    touch(tmp_path / "p.mp3")
    monkeypatch.setattr(tx, "_probe_duration_seconds", lambda mp3: 700.0)

    cfg1 = make_config(tmp_path, segment_seconds=480)
    pipeline.process_recording(mp4, cfg1)
    (tmp_path / "p.txt").unlink()  # force the stage to re-run

    work = tmp_path / "transcribe_work.p"
    manifest = work / "segments.json"
    import json as _json

    stored = _json.loads(manifest.read_text(encoding="utf-8"))
    assert stored["segment_seconds"] == 480

    # Count ffmpeg chunk calls on the second run only.
    ffmpeg_before = sum(
        1 for c in recorder.calls if c.argv[0] == "ffmpeg" and "-ss" in c.argv
    )
    cfg2 = make_config(tmp_path, segment_seconds=240)  # changed param
    pipeline.process_recording(mp4, cfg2)
    ffmpeg_after = sum(
        1 for c in recorder.calls if c.argv[0] == "ffmpeg" and "-ss" in c.argv
    )
    # The work dir was discarded and re-chunked (new ffmpeg -ss calls happened).
    assert ffmpeg_after > ffmpeg_before
    stored2 = _json.loads(manifest.read_text(encoding="utf-8"))
    assert stored2["segment_seconds"] == 240


def test_transcribe_existing_txt_skips_whole_stage(
    recorder: Recorder, seam: SeamRecorder, tmp_path: Path
) -> None:
    mp4 = touch(tmp_path / "done.mp4")
    touch(tmp_path / "done.mp3")
    touch(tmp_path / "done.txt")  # already transcribed
    cfg = make_config(tmp_path)

    result = pipeline.process_recording(mp4, cfg)

    assert seam.calls == []  # seam never invoked
    assert "txt" in result.skipped_artifacts


def test_slides_command_line_when_enabled(
    recorder: Recorder, seam: SeamRecorder, tmp_path: Path
) -> None:
    mp4 = touch(tmp_path / "lesson.mp4")
    cfg = make_config(tmp_path, slides=True)

    pipeline.process_recording(mp4, cfg)

    slide_calls = [c for c in recorder.calls if c.argv[0] == "scenedetect"]
    assert len(slide_calls) == 1
    call = slide_calls[0]
    assert call.argv == [
        "scenedetect",
        "-b",
        "pyav",
        "-i",
        str(mp4),
        "-o",
        str(tmp_path / "extracted_slides.lesson"),
        "detect-content",
        "--threshold",
        "30",
        "--min-scene-len",
        "5s",
        "list-scenes",
        "-f",
        "lesson.scenes.csv",
        "save-images",
        "-n",
        "1",
    ]
    assert call.timeout == 22


def test_slides_skipped_when_disabled(
    recorder: Recorder, seam: SeamRecorder, tmp_path: Path
) -> None:
    mp4 = touch(tmp_path / "lesson.mp4")
    cfg = make_config(tmp_path, slides=False)

    pipeline.process_recording(mp4, cfg)

    assert not any(c.argv[0] == "scenedetect" for c in recorder.calls)


def test_loaded_nested_slides_disabled_config_skips_scenedetect(
    recorder: Recorder, seam: SeamRecorder, tmp_path: Path
) -> None:
    """Regression (Copilot #2/#3): a LOADED nested ``stages.slides`` with
    ``enabled: false`` must not run scenedetect.

    ``SlidesStage`` is a dataclass, so the loaded object is always truthy; the
    pipeline must gate on ``.enabled``, not on the object's truthiness.
    """
    from transcriber import config as config_mod

    cfg_path = tmp_path / "host.config.yaml"
    cfg_path.write_text(
        "recordings_dir: {rec}\n"
        "stages:\n"
        "  slides:\n"
        "    enabled: false\n"
        "    backend: openrouter\n"
        "  s3_sync: false\n"
        "transcribe:\n"
        "  model_id: microsoft/mai-transcribe-2\n"
        "summary:\n"
        "  language: en\n"
        "  sections: [overview]\n"
        "  backend: agno\n"
        "agent:\n"
        "  output_file: '{{basename}}.md'\n"
        "openrouter:\n"
        "  api_key_env: OPENROUTER_API_KEY\n"
        "notion:\n"
        "  server: n\n"
        "  parent_page_id: p\n"
        "  insert: subpage\n"
        "  token_env: NOTION_API_KEY\n"
        "telegram:\n"
        "  bot_token_env: TELEGRAM_BOT_TOKEN\n"
        "  default_chat_id: '1'\n"
        "  routing: {{}}\n".format(rec=tmp_path.as_posix()),
        encoding="utf-8",
    )

    cfg = config_mod.load(str(cfg_path))
    # Sanity: the loaded object is a truthy dataclass with enabled=False.
    assert cfg.stages.slides.enabled is False
    assert bool(cfg.stages.slides) is True

    mp4 = touch(tmp_path / "lesson.mp4")
    pipeline.process_recording(mp4, cfg)

    assert not any(c.argv[0] == "scenedetect" for c in recorder.calls)


# --------------------------------------------------------------------------- #
# Idempotent per-artifact skip logic
# --------------------------------------------------------------------------- #
def test_all_artifacts_existing_is_noop(recorder: Recorder, tmp_path: Path) -> None:
    mp4 = touch(tmp_path / "done.mp4")
    touch(tmp_path / "done.mp3")
    touch(tmp_path / "done.txt")
    (tmp_path / "extracted_slides.done").mkdir()
    cfg = make_config(tmp_path, slides=True)

    result = pipeline.process_recording(mp4, cfg)

    assert recorder.calls == []
    assert result.new_artifacts == []
    assert set(result.skipped_artifacts) == {"mp3", "slides", "txt"}


def test_existing_mp3_skips_ffmpeg_only(
    recorder: Recorder, seam: SeamRecorder, tmp_path: Path
) -> None:
    mp4 = touch(tmp_path / "part.mp4")
    touch(tmp_path / "part.mp3")  # mp3 exists -> ffmpeg extract skipped, transcribe runs

    cfg = make_config(tmp_path)

    result = pipeline.process_recording(mp4, cfg)

    # No ffmpeg EXTRACT (-c:a) call; chunk_audio's ffmpeg -ss is fine.
    assert not any(
        c.argv[0] == "ffmpeg" and "-c:a" in c.argv for c in recorder.calls
    )
    assert len(seam.calls) == 1  # transcribe ran via the OpenRouter seam
    assert "mp3" in result.skipped_artifacts
    assert "txt" in result.new_artifacts


def test_existing_slides_dir_skips_scenedetect(
    recorder: Recorder, seam: SeamRecorder, tmp_path: Path
) -> None:
    mp4 = touch(tmp_path / "cls.mp4")
    (tmp_path / "extracted_slides.cls").mkdir()
    cfg = make_config(tmp_path, slides=True)

    result = pipeline.process_recording(mp4, cfg)

    assert not any(c.argv[0] == "scenedetect" for c in recorder.calls)
    assert "slides" in result.skipped_artifacts


# --------------------------------------------------------------------------- #
# New-recording detection over the recordings dir
# --------------------------------------------------------------------------- #
def test_process_all_detects_new_recordings(
    recorder: Recorder, seam: SeamRecorder, tmp_path: Path
) -> None:
    touch(tmp_path / "a.mp4")
    touch(tmp_path / "b.mp4")
    # b already has all outputs -> b is a no-op; a is new.
    touch(tmp_path / "b.mp3")
    touch(tmp_path / "b.txt")
    cfg = make_config(tmp_path)

    results = pipeline.process_all(cfg)

    by_name = {r.name: r for r in results}
    assert set(by_name) == {"a", "b"}
    assert by_name["a"].new_artifacts == ["mp3", "txt"]
    assert by_name["b"].new_artifacts == []
    # Deterministic ordering by file name.
    assert [r.name for r in results] == ["a", "b"]


def test_process_all_empty_dir(recorder: Recorder, tmp_path: Path) -> None:
    cfg = make_config(tmp_path)
    assert pipeline.process_all(cfg) == []


# --------------------------------------------------------------------------- #
# Timeout is passed through to every child-process call
# --------------------------------------------------------------------------- #
def test_timeouts_passed_through(
    recorder: Recorder, seam: SeamRecorder, tmp_path: Path
) -> None:
    mp4 = touch(tmp_path / "m.mp4")
    cfg = make_config(
        tmp_path,
        slides=True,
        timeouts=Timeouts(ffmpeg=5, scenedetect=6, transcribe=7, summarize=8, s3=9),
    )

    pipeline.process_recording(mp4, cfg)

    # ffmpeg (extract + chunk) uses the ffmpeg timeout; scenedetect its own.
    ffmpeg_timeouts = {c.timeout for c in recorder.calls if c.argv[0] == "ffmpeg"}
    assert ffmpeg_timeouts == {5}
    by_prog = {c.argv[0]: c.timeout for c in recorder.calls}
    assert by_prog["scenedetect"] == 6
    # The transcribe timeout is threaded to the seam.
    assert cfg.timeouts.transcribe == 7


# --------------------------------------------------------------------------- #
# _run_command itself: builds the right duct expression, enforces timeout.
# Here we mock the `duct` entrypoint (duct.cmd) to avoid spawning processes.
# --------------------------------------------------------------------------- #
class FakeHandle:
    def __init__(self, poll_results: list) -> None:
        self._poll_results = list(poll_results)
        self.killed = False
        self.waited = False

    def poll(self):
        if self._poll_results:
            return self._poll_results.pop(0)
        return object()  # finished

    def kill(self):
        self.killed = True

    def wait(self):
        self.waited = True
        return object()


class FakeExpression:
    def __init__(self, argv: tuple, handle: FakeHandle) -> None:
        self.argv = argv
        self.stdout_path_arg: Optional[str] = None
        self._handle = handle

    def stdout_path(self, path):
        self.stdout_path_arg = path
        return self

    def start(self):
        return self._handle


def test_run_command_builds_duct_expression(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handle = FakeHandle(poll_results=[])  # finishes immediately
    captured: dict = {}

    def fake_cmd(*argv):
        captured["argv"] = argv
        return FakeExpression(argv, handle)

    monkeypatch.setattr(pipeline.duct, "cmd", fake_cmd)

    pipeline._run_command(["ffmpeg", "-i", "x.mp4", "y.mp3"], timeout=900)

    assert captured["argv"] == ("ffmpeg", "-i", "x.mp4", "y.mp3")
    assert handle.killed is False


def test_run_command_applies_stdout_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    handle = FakeHandle(poll_results=[])
    expr = FakeExpression(("scenedetect",), handle)

    monkeypatch.setattr(pipeline.duct, "cmd", lambda *argv: expr)

    out = tmp_path / "o.txt"
    pipeline._run_command(["scenedetect"], timeout=900, stdout_path=out)

    assert expr.stdout_path_arg == str(out)


def test_run_command_enforces_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # poll always returns None (never finishes) -> timeout path.
    handle = FakeHandle(poll_results=[None] * 1000)

    monkeypatch.setattr(pipeline.duct, "cmd", lambda *argv: FakeExpression(argv, handle))
    monkeypatch.setattr(pipeline.time, "sleep", lambda _s: None)

    # Force the deadline to be immediately in the past.
    times = iter([0.0, 100.0, 100.0, 100.0])
    monkeypatch.setattr(pipeline.time, "monotonic", lambda: next(times))

    with pytest.raises(pipeline.PipelineTimeoutError) as excinfo:
        pipeline._run_command(["sleep", "1000"], timeout=1)

    assert handle.killed is True
    assert handle.waited is True
    assert excinfo.value.timeout == 1
    assert excinfo.value.argv == ["sleep", "1000"]
