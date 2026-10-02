"""Tests for transcriber.agent: prompt builder + agy invocation.

No real agy is invoked — ``agy_headless_bridge.run`` is monkeypatched.

The slide-description block now embeds pre-computed slide *markdown*
(``slides_markdown``) — the text of ``<name>.slides.md`` produced by the
``describe_slides`` stage — rather than listing image paths (Spec R3/R4).
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
    SlidesStage,
    Stages,
    Summary,
    Telegram,
    Timeouts,
    Transcribe,
)
from agy_headless_bridge import AgyTimeoutError

SECTIONS = ["Decisions", "Action Items", "Plans", "Identified Risks"]

# A representative slides.md payload (already in the required per-slide format).
SLIDES_MD = (
    "### Slide 1\n"
    "**Timestamp:** 00:00 - 00:30\n\n"
    "Title slide: project kickoff.\n\n"
    "### Slide 2\n"
    "**Timestamp:** 00:30 - 01:15\n\n"
    "Architecture diagram overview.\n"
)


def make_config(language: str = "en", sections=None) -> Config:
    return Config(
        recordings_dir="./recordings",
        stages=Stages(
            slides=SlidesStage(enabled=True, backend="openrouter"), s3_sync=False
        ),
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
    prompt = build_prompt(cfg, tp, slides_markdown=None)
    assert "English" in prompt
    assert "original language" in prompt  # the parenthetical translate note
    # No 'original-only' directive should dominate.
    assert "Write the ENTIRE summary" in prompt


def test_language_forced_original(tmp_path: Path) -> None:
    cfg = make_config(language="original")
    tp = write_transcript(tmp_path)
    prompt = build_prompt(cfg, tp, slides_markdown=None)
    assert "the original language spoken in the transcript." in prompt


def test_sections_come_from_config(tmp_path: Path) -> None:
    cfg = make_config(sections=["overview", "key_points", "action_items"])
    tp = write_transcript(tmp_path)
    prompt = build_prompt(cfg, tp, slides_markdown=None)
    assert "## Overview" in prompt
    assert "## Key Points" in prompt
    assert "## Action Items" in prompt
    # A section not in this config must not appear as a heading directive.
    assert "## Identified Risks" not in prompt


# --------------------------------------------------------------------------- #
# _slide_block / slide markdown embedding (Task 4)
# --------------------------------------------------------------------------- #
def test_slide_block_omitted_when_none() -> None:
    assert agent._slide_block(None) == ""


@pytest.mark.parametrize("empty", ["", "   ", "\n\t \n"])
def test_slide_block_omitted_when_empty_or_whitespace(empty: str) -> None:
    assert agent._slide_block(empty) == ""


def test_slide_block_embeds_markdown_when_non_empty() -> None:
    block = agent._slide_block(SLIDES_MD)
    assert block  # non-empty
    # The prepared markdown is embedded verbatim (as reference context).
    assert SLIDES_MD in block
    # Instruction references the Slide Descriptions section (appended by engine).
    assert "Slide Descriptions" in block
    # It is wrapped in a Markdown code fence (non-XML encapsulation).
    assert re.search(r"`{3,}\n" + re.escape(SLIDES_MD) + r"\n`{3,}", block)


def test_slide_block_tells_model_not_to_reproduce() -> None:
    """The slide block must instruct the model NOT to reproduce the slides.

    The engine appends the slide descriptions deterministically; the model
    echoing a large slide block previously truncated the summary before the
    Slide Descriptions section was reached.
    """
    block = agent._slide_block(SLIDES_MD)
    lowered = block.lower()
    assert "do not reproduce" in lowered or "not reproduce" in lowered
    # It must signal the engine appends them (reference-only framing).
    assert "reference" in lowered
    assert "append" in lowered


# --------------------------------------------------------------------------- #
# append_slide_descriptions — deterministic engine-side concatenation
# --------------------------------------------------------------------------- #
def test_append_slide_descriptions_appends_section() -> None:
    out = agent.append_slide_descriptions("# Summary\n\nBody.", SLIDES_MD)
    assert out.startswith("# Summary\n\nBody.")
    assert "## Slide Descriptions" in out
    # The slide markdown is appended verbatim after the heading.
    assert SLIDES_MD.strip() in out
    # Heading comes after the summary body.
    assert out.index("Body.") < out.index("## Slide Descriptions")


@pytest.mark.parametrize("empty", [None, "", "   ", "\n\t \n"])
def test_append_slide_descriptions_noop_when_empty(empty) -> None:
    out = agent.append_slide_descriptions("# Summary\n\nBody.", empty)
    assert "## Slide Descriptions" not in out
    assert out.rstrip() == "# Summary\n\nBody."


def test_append_slide_descriptions_single_copy() -> None:
    """Exactly one Slide Descriptions section is produced (no duplication)."""
    out = agent.append_slide_descriptions("# Summary", SLIDES_MD)
    assert out.count("## Slide Descriptions") == 1


def test_append_strips_slide_empty_markers_keeps_real() -> None:
    """[[SLIDE_EMPTY ...]] markers are stripped from the appended summary while
    the real slide descriptions are kept verbatim (markers stay in .slides.md,
    not in the reader-facing summary)."""
    slides_md = (
        "[[SLIDE_EMPTY 00:00 - 00:05]]\n\n"
        "### Real Slide\n**Timestamp:** 00:05 - 00:15\n\n- actual content\n\n"
        "[[SLIDE_EMPTY 00:15 - 00:20]]"
    )
    out = agent.append_slide_descriptions("# Summary\n\nBody.", slides_md)
    assert "## Slide Descriptions" in out
    assert "SLIDE_EMPTY" not in out
    assert "### Real Slide" in out
    assert "actual content" in out


def test_append_strips_fallback_placeholder_forms() -> None:
    """Stray placeholder forms (apology parenthetical, HTML comment, == form)
    are also stripped as a fallback net for a disobedient model."""
    slides_md = (
        "### Keep Me\n**Timestamp:** 00:00 - 00:05\n\n- real\n\n"
        "(No essential visual content — just a webcam view. Omitting per rules.)\n\n"
        "<!-- no information -->\n\n"
        "== no information =="
    )
    out = agent.append_slide_descriptions("# Summary", slides_md)
    assert "### Keep Me" in out
    assert "no essential visual content" not in out.lower()
    assert "no information" not in out.lower()


def test_append_omits_heading_when_all_slides_empty() -> None:
    """When every slide is an empty marker/placeholder, the Slide Descriptions
    heading is omitted entirely (no dangling empty section)."""
    slides_md = (
        "[[SLIDE_EMPTY 00:00 - 00:05]]\n\n"
        "[[SLIDE_EMPTY 00:05 - 00:10]]\n\n"
        "<!-- no information -->"
    )
    out = agent.append_slide_descriptions("# Summary\n\nBody.", slides_md)
    assert "## Slide Descriptions" not in out
    assert out.rstrip() == "# Summary\n\nBody."



def test_slide_block_has_no_image_paths() -> None:
    """The refactored slide block must NOT reference any image paths."""
    block = agent._slide_block(SLIDES_MD)
    assert ".png" not in block
    assert ".jpg" not in block
    assert ".jpeg" not in block
    assert "extracted_slides" not in block


def test_slide_block_fence_grows_past_backticks_in_markdown() -> None:
    """Slide markdown containing a triple-backtick block must be fenced by
    >=4 ticks so it cannot break out of its code fence."""
    md = "before\n```\ncode\n```\nafter"
    block = agent._slide_block(md)
    assert re.search(r"`{4,}\n" + re.escape(md) + r"\n`{4,}", block)


def test_build_prompt_includes_slide_block_when_markdown_present(
    tmp_path: Path,
) -> None:
    cfg = make_config()
    tp = write_transcript(tmp_path)

    no_slides = build_prompt(cfg, tp, slides_markdown=None)
    assert "Slide descriptions" not in no_slides
    assert "Slide Descriptions" not in no_slides

    with_slides = build_prompt(cfg, tp, slides_markdown=SLIDES_MD)
    assert "Slide descriptions" in with_slides
    assert SLIDES_MD in with_slides
    # No image paths threaded into the prompt anymore.
    assert ".png" not in with_slides
    assert ".jpg" not in with_slides


def test_build_prompt_omits_slide_block_on_whitespace_markdown(
    tmp_path: Path,
) -> None:
    cfg = make_config()
    tp = write_transcript(tmp_path)
    prompt = build_prompt(cfg, tp, slides_markdown="   \n\t  ")
    assert "Slide descriptions" not in prompt
    assert "Slide Descriptions" not in prompt


def test_transcript_wrapped_in_markdown_code_fence_not_xml(tmp_path: Path) -> None:
    text = "[00:00] we <should> not treat </these> as tags & data"
    tp = write_transcript(tmp_path, text)
    cfg = make_config()
    prompt = build_prompt(cfg, tp, slides_markdown=None)

    # The transcript text must be present verbatim.
    assert text in prompt

    # It must be wrapped by a triple-(or-more)-backtick fence.
    fence_match = re.search(r"(`{3,})\n" + re.escape(text) + r"\n\1", prompt)
    assert fence_match is not None, "transcript not enclosed in a backtick code fence"

    # The transcript block must NOT be wrapped by an XML/HTML tag such as
    # <transcript>...</transcript>. The only '<' characters near the block are
    # the literal ones inside the transcript data itself.
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
    prompt = build_prompt(cfg, tp, slides_markdown=None)
    # Outer fence must be at least 4 backticks and fully contain the text.
    assert re.search(r"`{4,}\n" + re.escape(text) + r"\n`{4,}", prompt)


def test_notion_publish_instructions_present(tmp_path: Path) -> None:
    cfg = make_config()
    tp = write_transcript(tmp_path)
    prompt = build_prompt(cfg, tp, slides_markdown=None)
    assert "PARENT-PAGE-ID-123" in prompt
    assert "notion-private" in prompt
    assert "subpage" in prompt
    assert "TOP" in prompt  # link goes at the top of the parent
    assert "meeting.md" in prompt  # output_file templated with {basename}


# --------------------------------------------------------------------------- #
# run_agent — agy mocked
# --------------------------------------------------------------------------- #
def test_digest_path_for_derives_telegram_file() -> None:
    from transcriber.agent import digest_path_for

    p = digest_path_for("/some/dir/2026-09-22_Meeting.md")
    assert p.name == "2026-09-22_Meeting.telegram.md"


def test_run_agent_writes_digest_instruction_and_path(tmp_path: Path, monkeypatch) -> None:
    """run_agent must instruct agy to write a concise Telegram digest to the
    ``<name>.telegram.md`` companion file, before the Notion step."""
    cfg = make_config()
    rec_dir = tmp_path / "rec"
    rec_dir.mkdir()
    tp = write_transcript(rec_dir)

    captured = {}

    def fake_run(prompt, *, add_dirs, extra_args, timeout, **kwargs):
        captured["prompt"] = prompt
        (rec_dir / "meeting.md").write_text("# Summary\n", encoding="utf-8")
        return ""

    monkeypatch.setattr(agent, "run", fake_run)
    run_agent(cfg, tp, rec_dir, slides_markdown=None)

    prompt = captured["prompt"]
    assert "meeting.telegram.md" in prompt
    assert "digest" in prompt.lower()
    assert prompt.index("meeting.telegram.md") < prompt.lower().index("publish to notion")


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

    out = run_agent(cfg, tp, rec_dir, slides_markdown=None)
    assert out == rec_dir / "meeting.md"
    assert out.read_text(encoding="utf-8").strip()

    # Bridge invoked with the required knobs.
    # Workspace is the recording dir's PARENT (the trusted host root), not the
    # untrusted recordings subdir — avoids agy's "trust this folder?" hang.
    assert captured["add_dirs"] == [str(rec_dir.resolve().parent)]
    assert captured["extra_args"] == ["--dangerously-skip-permissions"]
    assert captured["timeout"] == 42
    # idle_timeout tied to the hard ceiling so long agy runs aren't killed early.
    assert captured["idle_timeout"] == 42


def test_run_agent_embeds_slide_markdown_in_prompt(tmp_path: Path, monkeypatch) -> None:
    """run_agent threads slides_markdown into the prompt (no image paths)."""
    cfg = make_config()
    rec_dir = tmp_path / "rec"
    rec_dir.mkdir()
    tp = write_transcript(rec_dir)

    captured = {}

    def fake_run(prompt, *, add_dirs, extra_args, timeout, **kwargs):
        captured["prompt"] = prompt
        (rec_dir / "meeting.md").write_text("# Summary\n", encoding="utf-8")
        return ""

    monkeypatch.setattr(agent, "run", fake_run)
    run_agent(cfg, tp, rec_dir, slides_markdown=SLIDES_MD)

    prompt = captured["prompt"]
    assert SLIDES_MD in prompt
    assert "Slide Descriptions" in prompt
    assert ".png" not in prompt
    assert ".jpg" not in prompt


def test_run_agent_appends_slide_descriptions_to_file(tmp_path: Path, monkeypatch) -> None:
    """Split model: the summary file stays summary-only; the engine writes the
    cleaned slide descriptions to a separate <name>.slides-clean.md (markers
    stripped). agy itself writes only the summary."""
    cfg = make_config()
    rec_dir = tmp_path / "rec"
    rec_dir.mkdir()
    tp = write_transcript(rec_dir)

    def fake_run(prompt, *, add_dirs, extra_args, timeout, **kwargs):
        # Model writes ONLY the transcript summary (no slide block, per prompt).
        (rec_dir / "meeting.md").write_text(
            "# Summary\n\n## Overview\nStuff happened.\n", encoding="utf-8"
        )
        return ""

    monkeypatch.setattr(agent, "run", fake_run)
    out = run_agent(cfg, tp, rec_dir, slides_markdown=SLIDES_MD)

    # Summary file holds ONLY the summary — no slides appended.
    summary_text = out.read_text(encoding="utf-8")
    assert "Stuff happened." in summary_text
    assert "## Slide Descriptions" not in summary_text
    assert "Slide 1" not in summary_text

    # Cleaned slides live in the separate durable file.
    clean_path = out.with_suffix(".slides-clean.md")
    assert clean_path.exists()
    clean_text = clean_path.read_text(encoding="utf-8")
    assert SLIDES_MD.strip() in clean_text


def test_run_agent_strips_markers_in_clean_slides_file(tmp_path: Path, monkeypatch) -> None:
    """The <name>.slides-clean.md drops [[SLIDE_EMPTY]] markers, keeps real slides."""
    cfg = make_config()
    rec_dir = tmp_path / "rec"
    rec_dir.mkdir()
    tp = write_transcript(rec_dir)
    slides_md = (
        "[[SLIDE_EMPTY 00:00 - 00:05]]\n\n"
        "### Real\n**Timestamp:** 00:05 - 00:10\n\n- content"
    )

    def fake_run(prompt, *, add_dirs, extra_args, timeout, **kwargs):
        (rec_dir / "meeting.md").write_text("# Summary\n\nBody.\n", encoding="utf-8")
        return ""

    monkeypatch.setattr(agent, "run", fake_run)
    out = run_agent(cfg, tp, rec_dir, slides_markdown=slides_md)
    clean_text = out.with_suffix(".slides-clean.md").read_text(encoding="utf-8")
    assert "### Real" in clean_text
    assert "SLIDE_EMPTY" not in clean_text


def test_run_agent_no_slide_section_when_slides_absent(tmp_path: Path, monkeypatch) -> None:
    cfg = make_config()
    rec_dir = tmp_path / "rec"
    rec_dir.mkdir()
    tp = write_transcript(rec_dir)

    def fake_run(prompt, *, add_dirs, extra_args, timeout, **kwargs):
        (rec_dir / "meeting.md").write_text("# Summary\n\nBody.\n", encoding="utf-8")
        return ""

    monkeypatch.setattr(agent, "run", fake_run)
    out = run_agent(cfg, tp, rec_dir, slides_markdown=None)
    assert "## Slide Descriptions" not in out.read_text(encoding="utf-8")
    # No cleaned-slides file when there are no slides.
    assert not out.with_suffix(".slides-clean.md").exists()


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
    run_agent(cfg, tp, rec_dir, slides_markdown=None)

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
        run_agent(cfg, tp, rec_dir, slides_markdown=None)


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
        run_agent(cfg, tp, rec_dir, slides_markdown=None)


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

    out = run_agent(cfg, tp, rec_dir, slides_markdown=None)
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
        run_agent(cfg, tp, rec_dir, slides_markdown=None)
    assert excinfo.value.partial == "some partial work"

