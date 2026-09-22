"""Tests for transcriber.agent: prompt builder + agy invocation.

No real agy is invoked — ``agy_headless_bridge.run`` is monkeypatched.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

import transcriber.agent as agent
from transcriber.agent import build_prompt, run_agent
from transcriber.config import (
    Agent,
    Config,
    Notion,
    Stages,
    Summary,
    Telegram,
    Timeouts,
    Transcribe,
)
from agy_headless_bridge import AgyTimeoutError

SECTIONS = ["Decisions", "Action Items", "Plans", "Identified Risks"]


def make_config(language: str = "en", sections=None) -> Config:
    return Config(
        recordings_dir="./recordings",
        stages=Stages(slides=True, parse_transcript=True, s3_sync=False),
        transcribe=Transcribe(model_id="scribe_v1"),
        summary=Summary(language=language, sections=sections or list(SECTIONS)),
        agent=Agent(cli="agy", extra_args=[], output_file="{basename}.md"),
        notion=Notion(
            server="notion-private",
            parent_page_id="PARENT-PAGE-ID-123",
            insert="subpage",
        ),
        telegram=Telegram(
            bot_token_env="TELEGRAM_BOT_TOKEN",
            default_chat_id="123",
            routing={},
        ),
        timeouts=Timeouts(agy=42),
    )


def write_transcript(tmp_path: Path, text: str = "[00:00] hello world") -> Path:
    p = tmp_path / "meeting.txt"
    p.write_text(text, encoding="utf-8")
    return p


# --------------------------------------------------------------------------- #
# build_prompt units
# --------------------------------------------------------------------------- #
def test_language_forced_english(tmp_path: Path) -> None:
    cfg = make_config(language="en")
    tp = write_transcript(tmp_path)
    prompt = build_prompt(cfg, tp, slide_image_paths=[])
    assert "English" in prompt
    assert "original language" in prompt  # the parenthetical translate note
    # No 'original-only' directive should dominate.
    assert "Write the ENTIRE summary" in prompt


def test_language_forced_original(tmp_path: Path) -> None:
    cfg = make_config(language="original")
    tp = write_transcript(tmp_path)
    prompt = build_prompt(cfg, tp, slide_image_paths=[])
    assert "the original language spoken in the transcript." in prompt


def test_sections_come_from_config(tmp_path: Path) -> None:
    cfg = make_config(sections=["overview", "key_points", "action_items"])
    tp = write_transcript(tmp_path)
    prompt = build_prompt(cfg, tp, slide_image_paths=[])
    assert "## Overview" in prompt
    assert "## Key Points" in prompt
    assert "## Action Items" in prompt
    # A section not in this config must not appear as a heading directive.
    assert "## Identified Risks" not in prompt


def test_slide_sections_included_only_when_slides_exist(tmp_path: Path) -> None:
    cfg = make_config()
    tp = write_transcript(tmp_path)

    no_slides = build_prompt(cfg, tp, slide_image_paths=[])
    assert "Slide descriptions" not in no_slides
    assert "Slide Descriptions" not in no_slides
    assert "mermaid" not in no_slides.lower()

    with_slides = build_prompt(
        cfg, tp, slide_image_paths=["frames/slide-0001.png", "frames/slide-0002.png"]
    )
    assert "Slide descriptions" in with_slides
    assert "frames/slide-0001.png" in with_slides
    assert "frames/slide-0002.png" in with_slides
    assert "mermaid" in with_slides.lower()


def test_transcript_wrapped_in_markdown_code_fence_not_xml(tmp_path: Path) -> None:
    text = "[00:00] we <should> not treat </these> as tags & data"
    tp = write_transcript(tmp_path, text)
    cfg = make_config()
    prompt = build_prompt(cfg, tp, slide_image_paths=[])

    # The transcript text must be present verbatim.
    assert text in prompt

    # It must be wrapped by a triple-(or-more)-backtick fence.
    fence_match = re.search(r"(`{3,})\n" + re.escape(text) + r"\n\1", prompt)
    assert fence_match is not None, "transcript not enclosed in a backtick code fence"

    # The transcript block must NOT be wrapped by an XML/HTML tag such as
    # <transcript>...</transcript>. The only '<' characters near the block are
    # the literal ones inside the transcript data itself.
    # Assert no XML-tag wrapper immediately precedes/follows the fenced block.
    fence = fence_match.group(1)
    start = prompt.index(fence)
    # 40 chars of lead-in before the opening fence must contain no opening tag.
    lead_in = prompt[max(0, start - 40):start]
    assert not re.search(r"<[A-Za-z][\w:-]*\s*>\s*$", lead_in), (
        "transcript appears to be wrapped in an XML/HTML opening tag"
    )
    # And there must be no closing wrapper tag like </transcript> right after.
    tail = prompt[start:]
    closing = tail[tail.index(fence) + len(fence):]
    after_second_fence = closing[closing.index(fence) + len(fence):][:40]
    assert not re.search(r"^\s*</[A-Za-z][\w:-]*\s*>", after_second_fence), (
        "transcript appears to be wrapped in an XML/HTML closing tag"
    )


def test_fence_grows_past_backticks_in_transcript(tmp_path: Path) -> None:
    # A transcript containing a triple-backtick block must be fenced by >=4 ticks.
    text = "before\n```\ncode block\n```\nafter"
    tp = write_transcript(tmp_path, text)
    cfg = make_config()
    prompt = build_prompt(cfg, tp, slide_image_paths=[])
    # Outer fence must be at least 4 backticks and fully contain the text.
    assert re.search(r"`{4,}\n" + re.escape(text) + r"\n`{4,}", prompt)


def test_notion_publish_instructions_present(tmp_path: Path) -> None:
    cfg = make_config()
    tp = write_transcript(tmp_path)
    prompt = build_prompt(cfg, tp, slide_image_paths=[])
    assert "PARENT-PAGE-ID-123" in prompt
    assert "notion-private" in prompt
    assert "subpage" in prompt
    assert "TOP" in prompt  # link goes at the top of the parent
    assert "meeting.md" in prompt  # output_file templated with {basename}


# --------------------------------------------------------------------------- #
# run_agent — agy mocked
# --------------------------------------------------------------------------- #
def test_run_agent_reads_back_written_file(tmp_path: Path, monkeypatch) -> None:
    cfg = make_config()
    rec_dir = tmp_path / "rec"
    rec_dir.mkdir()
    tp = write_transcript(rec_dir)

    captured = {}

    def fake_run(prompt, *, add_dirs, extra_args, timeout, **kwargs):
        captured["prompt"] = prompt
        captured["add_dirs"] = add_dirs
        captured["extra_args"] = extra_args
        captured["timeout"] = timeout
        captured["idle_timeout"] = kwargs.get("idle_timeout")
        # Simulate agy writing the summary file.
        (rec_dir / "meeting.md").write_text("# Summary\n\nDecisions: none\n", encoding="utf-8")
        return "agy chatter on stdout (ignored)"

    monkeypatch.setattr(agent, "run", fake_run)

    out = run_agent(cfg, tp, rec_dir, slide_image_paths=[])
    assert out == rec_dir / "meeting.md"
    assert out.read_text(encoding="utf-8").strip()

    # Bridge invoked with the required knobs.
    assert captured["add_dirs"] == [str(rec_dir)]
    assert captured["extra_args"] == ["--dangerously-skip-permissions"]
    assert captured["timeout"] == 42
    # idle_timeout tied to the hard ceiling so long agy runs aren't killed early.
    assert captured["idle_timeout"] == 42


def test_run_agent_references_transcript_by_file_not_inline(tmp_path: Path, monkeypatch) -> None:
    """run_agent must NOT inline the transcript into the prompt (command line);
    it must instruct agy to read the transcript file by name. This avoids the
    Windows command-line length limit on large transcripts."""
    cfg = make_config()
    rec_dir = tmp_path / "rec"
    rec_dir.mkdir()
    big_transcript = "\n".join(f"[00:{i:02d}] " + "word " * 200 for i in range(60))
    tp = rec_dir / "meeting.txt"
    tp.write_text(big_transcript, encoding="utf-8")

    captured = {}

    def fake_run(prompt, *, add_dirs, extra_args, timeout, **kwargs):
        captured["prompt"] = prompt
        (rec_dir / "meeting.md").write_text("# Summary\n", encoding="utf-8")
        return ""

    monkeypatch.setattr(agent, "run", fake_run)
    run_agent(cfg, tp, rec_dir, slide_image_paths=[])

    prompt = captured["prompt"]
    # Names the transcript file so agy reads it.
    assert "meeting.txt" in prompt
    # Does not embed the (large) transcript body.
    assert "word word word" not in prompt
    # Prompt stays well under the Windows command-line limit (~32K).
    assert len(prompt) < 8000


def test_run_agent_raises_when_file_missing(tmp_path: Path, monkeypatch) -> None:
    cfg = make_config()
    rec_dir = tmp_path / "rec"
    rec_dir.mkdir()
    tp = write_transcript(rec_dir)

    monkeypatch.setattr(agent, "run", lambda *a, **k: "no file written")

    with pytest.raises(RuntimeError, match="did not write"):
        run_agent(cfg, tp, rec_dir, slide_image_paths=[])


def test_run_agent_raises_when_file_empty(tmp_path: Path, monkeypatch) -> None:
    cfg = make_config()
    rec_dir = tmp_path / "rec"
    rec_dir.mkdir()
    tp = write_transcript(rec_dir)

    def fake_run(*a, **k):
        (rec_dir / "meeting.md").write_text("   \n", encoding="utf-8")
        return ""

    monkeypatch.setattr(agent, "run", fake_run)

    with pytest.raises(RuntimeError, match="empty"):
        run_agent(cfg, tp, rec_dir, slide_image_paths=[])


def test_run_agent_timeout_with_partial_file_accepted(tmp_path: Path, monkeypatch) -> None:
    cfg = make_config()
    rec_dir = tmp_path / "rec"
    rec_dir.mkdir()
    tp = write_transcript(rec_dir)

    def fake_run(*a, **k):
        # agy wrote a partial-but-usable file before timing out.
        (rec_dir / "meeting.md").write_text("# Partial summary\n", encoding="utf-8")
        raise AgyTimeoutError("agy idle", partial="partial stdout")

    monkeypatch.setattr(agent, "run", fake_run)

    out = run_agent(cfg, tp, rec_dir, slide_image_paths=[])
    assert out.read_text(encoding="utf-8").strip() == "# Partial summary"


def test_run_agent_timeout_without_file_reraises_with_partial(tmp_path: Path, monkeypatch) -> None:
    cfg = make_config()
    rec_dir = tmp_path / "rec"
    rec_dir.mkdir()
    tp = write_transcript(rec_dir)

    def fake_run(*a, **k):
        raise AgyTimeoutError("agy idle", partial="some partial work")

    monkeypatch.setattr(agent, "run", fake_run)

    with pytest.raises(AgyTimeoutError) as excinfo:
        run_agent(cfg, tp, rec_dir, slide_image_paths=[])
    assert excinfo.value.partial == "some partial work"
