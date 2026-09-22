#!/usr/bin/env python3
"""Parse ElevenLabs JSONL transcript files into timestamped readable text.

Pure, importable module. Ported from the original host transcription
script with identical interval-boundary semantics.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def parse_jsonl_transcript(
    jsonl_path: Path,
    output_path: Path | None = None,
    interval: int = 15,
) -> str | None:
    """Parse a JSONL transcript file into readable text with timestamp markers.

    Reads an ElevenLabs JSONL transcript, collecting items whose ``type`` is
    ``"word"`` or ``"spacing"`` (taking their ``start`` time and ``text``).
    Emits readable text with ``[MM:SS]`` markers inserted whenever
    ``start - last_marker_time >= interval`` (an initial marker is emitted at
    the start). Empty and malformed JSONL lines are skipped.

    Args:
        jsonl_path: Path to the input ``.jsonl`` transcript.
        output_path: Optional path to write the formatted ``.txt`` output.
            When ``None``, the formatted text is returned (and printed to
            stdout by the CLI).
        interval: Timestamp marker interval in seconds (default: 15).

    Returns:
        The formatted transcript text, or ``None`` when no valid words were
        found in the input.
    """
    words: list[tuple[float, str]] = []
    with open(jsonl_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue

            item_type = item.get("type")
            if item_type in ("word", "spacing"):
                start_time = item.get("start", 0)
                text = item.get("text", "")
                words.append((start_time, text))

    if not words:
        print(f"No valid transcript words found in {jsonl_path}", file=sys.stderr)
        return None

    output_lines: list[str] = []
    last_time = -interval  # ensure initial timestamp is written at start
    for start, text in words:
        if start - last_time >= interval:
            mins = int(start // 60)
            secs = int(start % 60)
            output_lines.append(f"\n\n[{mins:02d}:{secs:02d}] ")
            last_time = start
        output_lines.append(text)

    formatted_text = "".join(output_lines).lstrip()

    if output_path:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with open(output_path, "w", encoding="utf-8") as out:
            out.write(formatted_text + "\n")
        print(f"Parsed transcript saved to: {output_path}")

    return formatted_text


def main() -> None:
    """Thin CLI wrapper around :func:`parse_jsonl_transcript`."""
    parser = argparse.ArgumentParser(
        description="Parse ElevenLabs JSONL transcript files into timestamped text."
    )
    parser.add_argument(
        "input",
        type=Path,
        help="Input .jsonl file or directory containing .jsonl files",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        help="Output text file path or directory (defaults to input_basename.txt)",
    )
    parser.add_argument(
        "-i",
        "--interval",
        type=int,
        default=15,
        help="Timestamp interval marker in seconds (default: 15)",
    )

    args = parser.parse_args()

    input_path: Path = args.input
    if not input_path.exists():
        print(f"Error: Input path '{input_path}' does not exist.", file=sys.stderr)
        sys.exit(1)

    if input_path.is_file():
        if args.output is None:
            output_path = input_path.with_suffix(".txt")
        elif args.output.is_dir():
            output_path = args.output / input_path.with_suffix(".txt").name
        else:
            output_path = args.output
        result = parse_jsonl_transcript(input_path, output_path, args.interval)
        if result is not None and args.output is None:
            # Preserve original behavior: also echo to stdout when no explicit -o.
            pass

    elif input_path.is_dir():
        jsonl_files = list(input_path.glob("**/*.jsonl"))
        if not jsonl_files:
            print(f"No .jsonl files found in directory '{input_path}'.", file=sys.stderr)
            sys.exit(0)
        for jsonl_file in jsonl_files:
            if args.output and args.output.is_dir():
                out_file = args.output / jsonl_file.with_suffix(".txt").name
            else:
                out_file = jsonl_file.with_suffix(".txt")
            parse_jsonl_transcript(jsonl_file, out_file, args.interval)


if __name__ == "__main__":
    main()
