"""Agent stage: build the summarization prompt and drive `agy` to summarize
the transcript and publish it to Notion.

Design constraints (see ``docs/tasks/transcriber-engine/task-4-agent-summarize-notion.md``):

* The summary language is forced per ``config.summary.language``.
* The section list comes from ``config.summary.sections``.
* Slide-description sections are included **only** when slides were extracted.
* Transcript content is wrapped using **non-XML** structural encapsulation
  (Markdown triple-backtick code fences) — never XML tags — to bound the
  prompt-injection surface.
* ``agy`` is the source of truth for the summary via the **file it writes**
  (``config.agent.output_file`` templated with ``{basename}``), *not* its stdout.
* Notion publishing is agent-driven: the prompt instructs agy to create a
  subpage under ``config.notion.parent_page_id`` via its notion MCP and add the
  subpage link at the **top** of the parent page.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional, Sequence

from agy_headless_bridge import AgyTimeoutError, run

from transcriber.config import Config

# Directory holding the Markdown prompt templates shipped with the package.
_TEMPLATES_DIR = Path(__file__).parent / "prompt_templates"

# Human-readable language directives keyed by ``config.summary.language``.
_LANGUAGE_INSTRUCTIONS = {
    "en": "English (translate from the original language if needed).",
    "original": "the original language spoken in the transcript.",
}


def _read_template(name: str) -> str:
    return (_TEMPLATES_DIR / name).read_text(encoding="utf-8")


def _language_instruction(language: str) -> str:
    return _LANGUAGE_INSTRUCTIONS.get(
        language, f"{language} (translate from the original language if needed)."
    )


def _sections_block(sections: Sequence[str]) -> str:
    """Render the configured section list as a Markdown checklist of headings."""
    lines = []
    for section in sections:
        title = section.replace("_", " ").strip().title()
        lines.append(f"- `## {title}`")
    return "\n".join(lines)


def _fence_for(text: str) -> str:
    """Return a backtick fence guaranteed longer than any run of backticks in
    ``text`` so the transcript cannot break out of its code fence.

    This keeps the encapsulation strictly Markdown (non-XML) while remaining
    robust to transcripts that themselves contain triple-backtick blocks.
    """
    longest = 0
    run_len = 0
    for ch in text:
        if ch == "`":
            run_len += 1
            longest = max(longest, run_len)
        else:
            run_len = 0
    return "`" * max(3, longest + 1)


def _transcript_fence(transcript: str) -> str:
    fence = _fence_for(transcript)
    # No language tag: this is plain data, not code to be highlighted.
    return f"{fence}\n{transcript}\n{fence}"


def _load_transcript(transcript_path: Path) -> str:
    return transcript_path.read_text(encoding="utf-8")


def _slide_block(slide_image_paths: Sequence[str]) -> str:
    """Assemble the slide-description block, included ONLY when slides exist.

    Returns an empty string when no slides were extracted, so the summary prompt
    contains no slide-description instructions at all.
    """
    if not slide_image_paths:
        return ""

    extractor = _read_template("slide_extractor.md")
    listed = "\n".join(f"- {p}" for p in slide_image_paths)
    return (
        "## Slide descriptions\n"
        "Slides were extracted for this recording. After the summary sections, "
        "append a **Slide Descriptions** section built from the following slide "
        "images (aligned to the transcript by timestamp):\n"
        f"{listed}\n\n"
        "Follow these per-slide extraction rules:\n\n"
        f"{extractor}"
    )


def build_prompt(
    config: Config,
    transcript_path: str | os.PathLike[str],
    slide_image_paths: Optional[Sequence[str]] = None,
    *,
    output_file: Optional[str] = None,
    inline_transcript: bool = True,
) -> str:
    """Assemble the agy prompt from the parsed transcript + slides + templates.

    Args:
        config: The loaded :class:`~transcriber.config.Config`.
        transcript_path: Path to the parsed ``.txt`` (or ``.jsonl``) transcript.
        slide_image_paths: Slide image paths. When empty/None, slide-description
            sections are omitted entirely.
        output_file: The resolved summary output filename agy must write. When
            omitted, it is derived from ``config.agent.output_file`` templated
            with the transcript ``{basename}``.
        inline_transcript: When True (default), the transcript text is embedded
            in the prompt (wrapped in a Markdown code fence). When False, the
            prompt instead instructs agy to READ the transcript file by path —
            required for real runs, since a full transcript embedded on the
            command line exceeds the Windows process command-line length limit.

    Returns:
        The fully-assembled prompt string. When inlined, the transcript is
        wrapped in a Markdown triple-backtick code fence — never XML tags.
    """
    transcript_path = Path(transcript_path)
    slide_image_paths = list(slide_image_paths or [])

    if output_file is None:
        basename = transcript_path.stem
        output_file = config.agent.output_file.format(basename=basename)

    if inline_transcript:
        transcript_section = _transcript_fence(_load_transcript(transcript_path))
    else:
        # Reference the file by path; agy reads it via its file tools (the
        # recording dir is exposed with --add-dir). Keeps the command line
        # short regardless of transcript size.
        transcript_section = (
            "Read the transcript from the file `"
            f"{transcript_path.name}` in the working directory. Do not expect "
            "it inline; open and read that file."
        )

    summary_template = _read_template("summary.md")
    body = summary_template.format(
        language_instruction=_language_instruction(config.summary.language),
        sections_block=_sections_block(config.summary.sections),
        slide_block=_slide_block(slide_image_paths),
        transcript_fence=transcript_section,
    )

    directives = (
        "# Task\n"
        "Summarize the meeting transcript and publish the result, following the "
        "instructions below exactly.\n\n"
        "## 1. Write the summary file\n"
        f"Write the complete Markdown summary to the file `{output_file}`. This "
        "file is the source of truth for the summary — do not rely on your "
        "stdout being read.\n\n"
        "## 2. Publish to Notion\n"
        f"Using your `{config.notion.server}` MCP, create a new "
        f"{config.notion.insert} under the parent page "
        f"`{config.notion.parent_page_id}` containing the summary. Then add a "
        "link to the newly created subpage at the **TOP** of the parent page.\n\n"
        "---\n\n"
    )

    return directives + body


def run_agent(
    config: Config,
    transcript_path: str | os.PathLike[str],
    recording_dir: str | os.PathLike[str],
    slide_image_paths: Optional[Sequence[str]] = None,
) -> Path:
    """Drive agy to summarize the transcript and publish to Notion, then read
    the summary file back.

    The summary comes from the file agy writes (``config.agent.output_file``
    templated with ``{basename}``), **not** from agy's stdout.

    Args:
        config: The loaded config.
        transcript_path: Path to the parsed transcript.
        recording_dir: Directory to expose to agy via ``--add-dir``. The output
            file is resolved relative to this directory.
        slide_image_paths: Slide image paths (slide sections included only when
            present).

    Returns:
        The path to the written summary file.

    Raises:
        RuntimeError: if the summary file is missing or empty after the run.
        AgyTimeoutError: re-raised (with ``.partial``) when agy times out.
    """
    transcript_path = Path(transcript_path)
    recording_dir = Path(recording_dir)

    basename = transcript_path.stem
    output_name = config.agent.output_file.format(basename=basename)
    output_path = Path(output_name)
    if not output_path.is_absolute():
        output_path = recording_dir / output_name

    prompt = build_prompt(
        config,
        transcript_path,
        slide_image_paths,
        output_file=output_name,
        inline_transcript=False,
    )

    try:
        run(
            prompt,
            add_dirs=[str(recording_dir)],
            extra_args=["--dangerously-skip-permissions"],
            timeout=config.timeouts.agy,
            # agy can think for a long stretch (summarize + Notion MCP call)
            # with no intermediate output. The bridge's default idle_timeout is
            # only 120s, which kills legitimate long runs; tie it to the hard
            # ceiling so the run is bounded solely by config.timeouts.agy.
            idle_timeout=config.timeouts.agy,
        )
    except AgyTimeoutError as exc:
        # Surface partial work: if agy still managed to write a non-empty
        # summary file before the kill, accept it; otherwise re-raise so the
        # caller can decide to resume.
        if output_path.exists() and output_path.read_text(encoding="utf-8").strip():
            return output_path
        # Attach partial context by re-raising the original error (it carries
        # ``.partial`` already).
        raise

    if not output_path.exists():
        raise RuntimeError(
            f"agy did not write the expected summary file: {output_path}"
        )
    if not output_path.read_text(encoding="utf-8").strip():
        raise RuntimeError(f"agy wrote an empty summary file: {output_path}")

    return output_path
