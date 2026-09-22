"""Golden-file and behavior tests for transcriber.parse."""

from __future__ import annotations

import json
from pathlib import Path

from transcriber.parse import parse_jsonl_transcript

FIXTURES = Path(__file__).parent / "fixtures"
SAMPLE = FIXTURES / "sample.jsonl"
EXPECTED = FIXTURES / "expected.txt"


def test_golden_file_output(tmp_path: Path) -> None:
    out = tmp_path / "out.txt"
    result = parse_jsonl_transcript(SAMPLE, out, interval=15)

    expected_text = EXPECTED.read_text(encoding="utf-8")
    # Function returns the formatted text (without the trailing newline the
    # file gets); the written file includes the trailing newline.
    assert result is not None
    assert result + "\n" == expected_text
    assert out.read_text(encoding="utf-8") == expected_text


def test_returns_text_without_output_path() -> None:
    result = parse_jsonl_transcript(SAMPLE, None, interval=15)
    assert result == EXPECTED.read_text(encoding="utf-8").rstrip("\n")


def test_skips_empty_and_malformed_lines(tmp_path: Path) -> None:
    src = tmp_path / "in.jsonl"
    src.write_text(
        "\n"
        "   \n"
        "not json at all\n"
        '{"type": "word", "start": 0.0, "text": "ok"}\n'
        "{bad json}\n"
        '{"type": "spacing", "start": 0.2, "text": "!"}\n',
        encoding="utf-8",
    )
    result = parse_jsonl_transcript(src, None, interval=15)
    assert result == "[00:00] ok!"


def test_ignores_non_word_spacing_types(tmp_path: Path) -> None:
    src = tmp_path / "in.jsonl"
    src.write_text(
        '{"type": "audio_event", "start": 0.0, "text": "(music)"}\n'
        '{"type": "word", "start": 0.0, "text": "hi"}\n',
        encoding="utf-8",
    )
    result = parse_jsonl_transcript(src, None, interval=15)
    assert result == "[00:00] hi"


def test_interval_boundary_is_gte(tmp_path: Path) -> None:
    # start - last_time >= interval must emit a marker exactly at the boundary.
    src = tmp_path / "in.jsonl"
    lines = [
        {"type": "word", "start": 0.0, "text": "a"},
        {"type": "word", "start": 4.9, "text": "b"},  # 4.9 < 5 -> no marker
        {"type": "word", "start": 5.0, "text": "c"},  # 5.0 >= 5 -> marker
    ]
    src.write_text("\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8")
    result = parse_jsonl_transcript(src, None, interval=5)
    assert result == "[00:00] ab\n\n[00:05] c"


def test_no_valid_words_returns_none(tmp_path: Path) -> None:
    src = tmp_path / "empty.jsonl"
    src.write_text("\n\nnot json\n", encoding="utf-8")
    assert parse_jsonl_transcript(src, None, interval=15) is None


def test_minutes_formatting(tmp_path: Path) -> None:
    src = tmp_path / "in.jsonl"
    lines = [
        {"type": "word", "start": 0.0, "text": "x"},
        {"type": "word", "start": 125.4, "text": "y"},  # 02:05
    ]
    src.write_text("\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8")
    result = parse_jsonl_transcript(src, None, interval=15)
    assert result == "[00:00] x\n\n[02:05] y"
