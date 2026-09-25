"""Tests for the engine-side direct Notion publish (``notion_publish``)."""

from __future__ import annotations

import pytest

from transcriber.backends.errors import SummarizeError
from transcriber.backends.notion_publish import (
    extract_page_id,
    markdown_to_blocks,
)


# --------------------------------------------------------------------------- #
# extract_page_id
# --------------------------------------------------------------------------- #
def test_extract_from_hyphenated_uuid():
    pid = "3e541441-7aa3-8019-80c0-f402a9386a52"
    assert extract_page_id(pid) == pid


def test_extract_from_bare_hex():
    assert (
        extract_page_id("3e5414417aa3801980c0f402a9386a52")
        == "3e541441-7aa3-8019-80c0-f402a9386a52"
    )


def test_extract_from_notion_url():
    url = (
        "https://app.notion.com/p/flyvercity/"
        "Transcriptions-3e5414417aa3801980c0f402a9386a52"
    )
    assert extract_page_id(url) == "3e541441-7aa3-8019-80c0-f402a9386a52"


def test_extract_uppercase_hex_lowercased():
    assert (
        extract_page_id("3E5414417AA3801980C0F402A9386A52")
        == "3e541441-7aa3-8019-80c0-f402a9386a52"
    )


def test_extract_empty_raises():
    with pytest.raises(SummarizeError, match="empty"):
        extract_page_id("")


def test_extract_no_id_raises():
    with pytest.raises(SummarizeError, match="could not extract"):
        extract_page_id("no-id-here-at-all")


# --------------------------------------------------------------------------- #
# markdown_to_blocks — losing nothing
# --------------------------------------------------------------------------- #
def _types(blocks):
    return [b["type"] for b in blocks]


def _text(block):
    rt = block[block["type"]]["rich_text"]
    return "".join(t["text"]["content"] for t in rt)


def test_headings_map_to_heading_blocks():
    blocks = markdown_to_blocks("# H1\n## H2\n### H3\n#### H4")
    assert _types(blocks) == [
        "heading_1",
        "heading_2",
        "heading_3",
        "heading_3",  # deeper headings clamp to heading_3
    ]
    assert _text(blocks[0]) == "H1"


def test_bullets_and_numbers():
    blocks = markdown_to_blocks("- a\n* b\n+ c\n1. one\n2) two")
    assert _types(blocks) == [
        "bulleted_list_item",
        "bulleted_list_item",
        "bulleted_list_item",
        "numbered_list_item",
        "numbered_list_item",
    ]
    assert _text(blocks[3]) == "one"


def test_quote_and_code_and_paragraph():
    md = "> quoted\n```\ncode line 1\ncode line 2\n```\nplain paragraph"
    blocks = markdown_to_blocks(md)
    assert _types(blocks) == ["quote", "code", "paragraph"]
    assert _text(blocks[1]) == "code line 1\ncode line 2"
    assert _text(blocks[2]) == "plain paragraph"


def test_blank_lines_skipped_but_text_preserved():
    md = "para one\n\n\npara two"
    blocks = markdown_to_blocks(md)
    assert _types(blocks) == ["paragraph", "paragraph"]
    assert _text(blocks[0]) == "para one"
    assert _text(blocks[1]) == "para two"


def test_inline_markers_kept_verbatim_lose_nothing():
    # We do not strip **bold** — the characters must survive.
    blocks = markdown_to_blocks("- **Decision:** ship it")
    assert _text(blocks[0]) == "**Decision:** ship it"


def test_long_paragraph_chunked_into_rich_text_objects():
    long_line = "x" * 4500  # > 2000-char per-object cap, single paragraph line
    blocks = markdown_to_blocks(long_line)
    assert len(blocks) == 1
    rt = blocks[0]["paragraph"]["rich_text"]
    # 4500 chars -> 3 chunks (2000 + 2000 + 500), nothing lost.
    assert len(rt) == 3
    assert "".join(t["text"]["content"] for t in rt) == long_line
