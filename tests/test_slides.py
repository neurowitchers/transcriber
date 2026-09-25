"""Tests for transcriber.backends.slides: ``build_slide_inputs`` helper plus the
``OpenRouterSlidesBackend`` and ``AgySlidesBackend`` describe_slides backends.

No real network, no real ``agy``, no real image decoding:

* ``_downscale_jpeg_to_bytes`` is monkeypatched so tests need no real JPEGs.
* ``_openrouter_vision`` is monkeypatched at the module seam.
* ``run_agent`` is monkeypatched at the module seam.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

import transcriber.backends.slides as slides_mod
import transcriber.pipeline as pipeline_mod
from transcriber.backends import (
    AgySlidesBackend,
    OpenRouterSlidesBackend,
    SlideDescribeError,
    SlideInput,
    build_slide_inputs,
)
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
SLIDES_MODEL = "google/gemini-2.0-flash-001"


# --------------------------------------------------------------------------- #
# Fixtures / helpers
# --------------------------------------------------------------------------- #
def make_config(*, with_openrouter: bool = True) -> Config:
    return Config(
        recordings_dir="./recordings",
        stages=Stages(
            slides=SlidesStage(enabled=True, backend="openrouter"), s3_sync=False
        ),
        transcribe=Transcribe(model_id="scribe_v1"),
        summary=Summary(language="en", sections=["overview"]),
        agent=Agent(cli="agy", extra_args=[], output_file="{basename}.md"),
        notion=Notion(server="notion", parent_page_id="P", insert="subpage"),
        telegram=Telegram(
            bot_token_env="TELEGRAM_BOT_TOKEN", default_chat_id="1", routing={}
        ),
        timeouts=Timeouts(),
        openrouter=(
            OpenRouter(api_key_env=API_KEY_ENV, slides_model=SLIDES_MODEL)
            if with_openrouter
            else None
        ),
    )


@pytest.fixture(autouse=True)
def _fake_downscale(monkeypatch: pytest.MonkeyPatch) -> None:
    """Avoid real image decoding: return small deterministic bytes."""
    monkeypatch.setattr(
        slides_mod,
        "_downscale_jpeg_to_bytes",
        lambda path, max_long_edge: b"JPEGDATA-" + path.name.encode(),
    )


# Standard scenedetect list-scenes CSV: a leading cut-list row, a header row,
# then per-scene rows. Timecodes are HH:MM:SS.mmm.
SCENES_CSV = (
    "Timecode List:,00:00:05.000,00:00:20.000\n"
    "Scene Number,Start Frame,Start Timecode,Start Time (seconds),"
    "End Frame,End Timecode,End Time (seconds),Length (frames),"
    "Length (timecode),Length (seconds)\n"
    "1,0,00:00:00.000,0.000,150,00:00:05.000,5.000,150,00:00:05.000,5.000\n"
    "2,150,00:00:05.000,5.000,600,00:01:10.000,70.000,450,00:00:20.000,20.000\n"
)


def make_slides_dir(
    tmp_path: Path, name: str, n_images: int, *, csv_text: str | None = SCENES_CSV
) -> Path:
    """Create ``extracted_slides.<name>/`` with ``n_images`` jpgs + optional CSV."""
    slides_dir = tmp_path / f"extracted_slides.{name}"
    slides_dir.mkdir(parents=True)
    for i in range(n_images):
        (slides_dir / f"scene-{i + 1:03d}.jpg").write_bytes(b"\xff\xd8fake")
    if csv_text is not None:
        (slides_dir / f"{name}.scenes.csv").write_text(csv_text, encoding="utf-8")
    return slides_dir


# --------------------------------------------------------------------------- #
# build_slide_inputs
# --------------------------------------------------------------------------- #
def test_build_slide_inputs_reads_csv_from_slides_dir_stable_order(
    tmp_path: Path,
) -> None:
    name = "rec1"
    make_slides_dir(tmp_path, name, 2)
    inputs = build_slide_inputs(tmp_path, name)

    assert [p.image_path.name for p in inputs] == [
        "scene-001.jpg",
        "scene-002.jpg",
    ]
    # 00:00:00 - 00:00:05 -> 00:00 - 00:05 ; 00:00:05 - 00:01:10 -> 00:05 - 01:10
    assert inputs[0].timestamp == "00:00 - 00:05"
    assert inputs[1].timestamp == "00:05 - 01:10"


def test_build_slide_inputs_unknown_on_missing_csv_with_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    name = "rec2"
    make_slides_dir(tmp_path, name, 2, csv_text=None)  # no CSV
    with caplog.at_level(logging.WARNING, logger=slides_mod.logger.name):
        inputs = build_slide_inputs(tmp_path, name)

    assert [p.timestamp for p in inputs] == ["unknown", "unknown"]
    combined = "\n".join(r.getMessage() for r in caplog.records)
    assert "unparseable" in combined or "missing" in combined


def test_build_slide_inputs_unknown_on_unparseable_csv(tmp_path: Path) -> None:
    name = "rec3"
    make_slides_dir(tmp_path, name, 1, csv_text="garbage,not,a,scenes,csv\n")
    inputs = build_slide_inputs(tmp_path, name)
    assert inputs[0].timestamp == "unknown"


def test_build_slide_inputs_more_images_than_rows_pads_unknown(
    tmp_path: Path,
) -> None:
    name = "rec4"
    # CSV describes 2 scenes but 3 images exist.
    make_slides_dir(tmp_path, name, 3)
    inputs = build_slide_inputs(tmp_path, name)
    assert [p.timestamp for p in inputs] == [
        "00:00 - 00:05",
        "00:05 - 01:10",
        "unknown",
    ]


def test_build_slide_inputs_zero_images_returns_empty(tmp_path: Path) -> None:
    name = "empty"
    (tmp_path / f"extracted_slides.{name}").mkdir()
    assert build_slide_inputs(tmp_path, name) == []


def test_build_slide_inputs_no_slides_dir_returns_empty(tmp_path: Path) -> None:
    assert build_slide_inputs(tmp_path, "nope") == []


# --------------------------------------------------------------------------- #
# OpenRouterSlidesBackend
# --------------------------------------------------------------------------- #
def _capture_vision(monkeypatch: pytest.MonkeyPatch, return_value: str = "MD"):
    calls: list[dict] = []

    def fake_vision(config, messages, model, *, timeout):
        calls.append({"messages": messages, "model": model, "timeout": timeout})
        return return_value

    monkeypatch.setattr(slides_mod, "_openrouter_vision", fake_vision)
    return calls


def test_openrouter_request_shape_one_image_url_per_slide_ts_before(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _capture_vision(monkeypatch, "## Slides\nok")
    slides = [
        SlideInput(image_path=tmp_path / "a.jpg", timestamp="00:00 - 00:05"),
        SlideInput(image_path=tmp_path / "b.jpg", timestamp="00:05 - 01:10"),
    ]
    result = OpenRouterSlidesBackend().describe(
        slides, "transcript body", make_config(), timeout=30
    )
    assert result == "## Slides\nok"
    assert len(calls) == 1
    assert calls[0]["model"] == SLIDES_MODEL
    assert calls[0]["timeout"] == 30

    content = calls[0]["messages"][0]["content"]
    # One leading text part.
    assert content[0]["type"] == "text"
    # Then per slide: Timestamp text part immediately before its image_url.
    image_parts = [c for c in content if c["type"] == "image_url"]
    assert len(image_parts) == 2  # one image_url per slide

    # Verify each image_url is immediately preceded by its Timestamp text part.
    for i, part in enumerate(content):
        if part["type"] == "image_url":
            prev = content[i - 1]
            assert prev["type"] == "text"
            assert prev["text"].startswith("Timestamp: ")
            assert part["image_url"]["url"].startswith("data:image/jpeg;base64,")

    # Timestamps present and in order.
    ts_texts = [c["text"] for c in content if c.get("text", "").startswith("Timestamp:")]
    assert ts_texts == ["Timestamp: 00:00 - 00:05", "Timestamp: 00:05 - 01:10"]


def test_openrouter_empty_slides_no_http_returns_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _capture_vision(monkeypatch, "should not be used")
    result = OpenRouterSlidesBackend().describe([], "t", make_config(), timeout=5)
    assert result == ""
    assert calls == []  # no HTTP call


def test_openrouter_valid_empty_response_returns_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _capture_vision(monkeypatch, "   \n  ")  # whitespace-only
    slides = [SlideInput(image_path=tmp_path / "a.jpg", timestamp="unknown")]
    result = OpenRouterSlidesBackend().describe(slides, "t", make_config(), timeout=5)
    assert result == ""


def test_openrouter_over_slide_ceiling_raises_before_http(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _capture_vision(monkeypatch)
    slides = [
        SlideInput(image_path=tmp_path / f"s{i}.jpg", timestamp="unknown")
        for i in range(slides_mod.MAX_SLIDES + 1)
    ]
    with pytest.raises(SlideDescribeError) as exc:
        OpenRouterSlidesBackend().describe(slides, "t", make_config(), timeout=5)
    assert "maximum" in str(exc.value)
    assert calls == []  # no HTTP call before the ceiling raise


def test_openrouter_raised_slide_ceiling_allows_bigger_deck(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _capture_vision(monkeypatch, "described")
    cfg = make_config()
    cfg.openrouter.max_slides = slides_mod.MAX_SLIDES + 25  # raise the ceiling
    slides = [
        SlideInput(image_path=tmp_path / f"s{i}.jpg", timestamp="unknown")
        for i in range(slides_mod.MAX_SLIDES + 1)  # over default, under raised
    ]
    result = OpenRouterSlidesBackend().describe(slides, "t", cfg, timeout=5)
    assert result == "described"
    assert len(calls) == 1  # the ceiling did NOT trip; one HTTP call made


def test_openrouter_over_byte_ceiling_raises_before_http(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _capture_vision(monkeypatch)
    # Make each downscaled image huge so the byte ceiling trips quickly.
    big = b"x" * (slides_mod.MAX_TOTAL_ENCODED_BYTES // 2 + 1024)
    monkeypatch.setattr(
        slides_mod, "_downscale_jpeg_to_bytes", lambda path, m: big
    )
    slides = [
        SlideInput(image_path=tmp_path / "a.jpg", timestamp="unknown"),
        SlideInput(image_path=tmp_path / "b.jpg", timestamp="unknown"),
    ]
    with pytest.raises(SlideDescribeError) as exc:
        OpenRouterSlidesBackend().describe(slides, "t", make_config(), timeout=5)
    assert "encoded payload" in str(exc.value)
    assert calls == []


def test_openrouter_missing_config_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _capture_vision(monkeypatch)
    slides = [SlideInput(image_path=tmp_path / "a.jpg", timestamp="unknown")]
    with pytest.raises(SlideDescribeError):
        OpenRouterSlidesBackend().describe(
            slides, "t", make_config(with_openrouter=False), timeout=5
        )


def test_openrouter_logs_no_image_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _capture_vision(monkeypatch, "ok")
    secret_bytes = b"SUPER-SECRET-IMAGE-BYTES"
    monkeypatch.setattr(
        slides_mod, "_downscale_jpeg_to_bytes", lambda path, m: secret_bytes
    )
    slides = [SlideInput(image_path=tmp_path / "a.jpg", timestamp="00:00 - 00:05")]
    with caplog.at_level(logging.DEBUG, logger=slides_mod.logger.name):
        OpenRouterSlidesBackend().describe(slides, "t", make_config(), timeout=5)
    combined = "\n".join(r.getMessage() for r in caplog.records)
    assert "SUPER-SECRET-IMAGE-BYTES" not in combined


def test_openrouter_uses_slides_model_not_summary_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _capture_vision(monkeypatch, "ok")
    config = make_config()
    slides = [SlideInput(image_path=tmp_path / "a.jpg", timestamp="unknown")]
    OpenRouterSlidesBackend().describe(slides, "t", config, timeout=5)
    assert calls[0]["model"] == config.openrouter.slides_model
    assert calls[0]["model"] != config.openrouter.summary_model


# --------------------------------------------------------------------------- #
# AgySlidesBackend
# --------------------------------------------------------------------------- #
def test_agy_backend_writes_and_returns_markdown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    name = "rec"
    slides_dir = tmp_path / f"extracted_slides.{name}"
    slides_dir.mkdir()
    (slides_dir / "a.jpg").write_bytes(b"x")

    written = tmp_path / f"{name}.slides.md"

    def fake_run_agent(config, transcript_path, recording_dir, image_paths):
        # agy would write the slide markdown; simulate it.
        written.write_text("## Slide Descriptions\ndone", encoding="utf-8")
        # image paths are passed through.
        assert image_paths == [str(slides_dir / "a.jpg")]
        return written

    monkeypatch.setattr(slides_mod, "run_agent", fake_run_agent)

    slides = [SlideInput(image_path=slides_dir / "a.jpg", timestamp="00:00 - 00:05")]
    result = AgySlidesBackend().describe(slides, "transcript", make_config(), timeout=5)
    assert result == "## Slide Descriptions\ndone"


def test_agy_backend_empty_slides_returns_empty_no_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail(*a, **k):  # pragma: no cover - must not be called
        raise AssertionError("run_agent should not be called for empty slides")

    monkeypatch.setattr(slides_mod, "run_agent", fail)
    assert AgySlidesBackend().describe([], "t", make_config(), timeout=5) == ""


# --------------------------------------------------------------------------- #
# Pipeline stays network-free (no slides-backend call)
# --------------------------------------------------------------------------- #
def test_pipeline_process_recording_makes_no_slides_backend_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Fail loudly if either backend or the vision helper is invoked by the pipeline.
    def boom(*a, **k):  # pragma: no cover - must not be called
        raise AssertionError("pipeline must not call a slides backend")

    monkeypatch.setattr(slides_mod, "_openrouter_vision", boom)
    monkeypatch.setattr(slides_mod, "run_agent", boom)
    monkeypatch.setattr(
        OpenRouterSlidesBackend, "describe", lambda *a, **k: boom()
    )
    monkeypatch.setattr(AgySlidesBackend, "describe", lambda *a, **k: boom())

    # Stub the pipeline's child-process seam so no real binaries run.
    ran: list[list[str]] = []

    def fake_run_command(argv, timeout, stdout_path=None):
        ran.append(argv)
        if stdout_path is not None:
            Path(stdout_path).write_text(
                '{"text": "transcript"}', encoding="utf-8"
            )

    monkeypatch.setattr(pipeline_mod, "_run_command", fake_run_command)

    recordings = tmp_path / "recordings"
    recordings.mkdir()
    mp4 = recordings / "rec.mp4"
    mp4.write_bytes(b"\x00")

    config = make_config()
    object.__setattr__(config, "recordings_dir", str(recordings))

    result = pipeline_mod.process_recording(mp4, config)
    # Pipeline ran (produced artifacts) without any slides-backend involvement.
    assert result.name == "rec"
    assert ran  # ffmpeg / scenedetect / transcribe invoked, no backend
