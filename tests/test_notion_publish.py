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




# --------------------------------------------------------------------------- #
# Bounded request timeout (Copilot #5)
# --------------------------------------------------------------------------- #
def _make_config_for_publish():
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

    return Config(
        recordings_dir="./recordings",
        stages=Stages(slides=SlidesStage(enabled=False), s3_sync=False),
        transcribe=Transcribe(model_id="scribe_v1"),
        summary=Summary(language="en", sections=["overview"], backend="agno"),
        agent=Agent(cli="agy", extra_args=[], output_file="{basename}.md"),
        notion=Notion(
            server="n",
            parent_page_id="3e5414417aa3801980c0f402a9386a52",
            insert="subpage",
            token_env="MY_NOTION_TOKEN",
        ),
        telegram=Telegram(bot_token_env="TG", default_chat_id="c", routing={}),
        timeouts=Timeouts(),
        openrouter=OpenRouter(api_key_env="OR_KEY"),
    )


def test_publish_passes_bounded_timeout_to_urlopen(monkeypatch):
    """``publish_to_notion`` must pass a bounded ``timeout`` to ``urlopen``."""
    import transcriber.backends.notion_publish as np

    monkeypatch.setenv("MY_NOTION_TOKEN", "ntn_secret")
    seen: list[float] = []

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b'{"id": "page-123", "url": "https://notion.so/page-123"}'

    def fake_urlopen(req, timeout=None):
        seen.append(timeout)
        return _Resp()

    monkeypatch.setattr(np.urllib.request, "urlopen", fake_urlopen)

    cfg = _make_config_for_publish()
    url = np.publish_to_notion(cfg, "Title", "# H1\n\nBody.", timeout=12.5)
    assert url == "https://notion.so/page-123"
    # Every request carried the bounded timeout (not None / unbounded).
    assert seen and all(t == 12.5 for t in seen)


def test_publish_wraps_timeout_error_as_summarize_error(monkeypatch):
    """A socket ``TimeoutError`` must surface as a retryable ``SummarizeError``."""
    import transcriber.backends.notion_publish as np

    monkeypatch.setenv("MY_NOTION_TOKEN", "ntn_secret")

    def fake_urlopen(req, timeout=None):
        raise TimeoutError("timed out")

    monkeypatch.setattr(np.urllib.request, "urlopen", fake_urlopen)

    cfg = _make_config_for_publish()
    with pytest.raises(SummarizeError, match="timed out"):
        np.publish_to_notion(cfg, "Title", "# H1\n\nBody.", timeout=1.0)


def test_default_notion_timeout_is_bounded():
    from transcriber.backends.notion_publish import DEFAULT_NOTION_TIMEOUT

    assert isinstance(DEFAULT_NOTION_TIMEOUT, (int, float))
    assert 0 < DEFAULT_NOTION_TIMEOUT < 3600


# --------------------------------------------------------------------------- #
# Slides child subpage
# --------------------------------------------------------------------------- #
class _CaptureResp:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self._body


def _install_capturing_urlopen(monkeypatch):
    """Patch urlopen to record (method, url, parsed-payload) and return new ids."""
    import json as _json
    import transcriber.backends.notion_publish as np

    calls: list[dict] = []
    counter = {"n": 0}

    def fake_urlopen(req, timeout=None):
        counter["n"] += 1
        payload = _json.loads(req.data.decode()) if req.data else None
        calls.append(
            {"method": req.get_method(), "url": req.full_url, "payload": payload}
        )
        pid = f"page-{counter['n']}"
        body = (
            '{"id": "' + pid + '", "url": "https://notion.so/' + pid + '"}'
        ).encode()
        return _CaptureResp(body)

    monkeypatch.setattr(np.urllib.request, "urlopen", fake_urlopen)
    return calls


def test_publish_creates_slides_child_subpage(monkeypatch):
    """When slides_markdown is supplied, a 'Slide Descriptions' child page is
    created UNDER the summary page (parent = summary page id)."""
    import transcriber.backends.notion_publish as np

    monkeypatch.setenv("MY_NOTION_TOKEN", "ntn_secret")
    calls = _install_capturing_urlopen(monkeypatch)

    cfg = _make_config_for_publish()
    url = np.publish_to_notion(
        cfg,
        "My Meeting",
        "# Summary\n\nBody.",
        slides_markdown="### Slide 1\n\n- a visual",
        timeout=10.0,
    )
    assert url == "https://notion.so/page-1"  # summary page URL returned

    # Two page-creation POSTs: summary (under parent), slides (under summary).
    create_posts = [
        c for c in calls if c["method"] == "POST" and c["url"].endswith("/pages")
    ]
    assert len(create_posts) == 2

    summary_post, slides_post = create_posts
    # Summary page is parented to the configured parent page id.
    assert summary_post["payload"]["parent"]["page_id"] == (
        "3e541441-7aa3-8019-80c0-f402a9386a52"
    )
    assert summary_post["payload"]["properties"]["title"]["title"][0]["text"][
        "content"
    ] == "My Meeting"

    # Slides child page is parented to the SUMMARY page (page-1), titled
    # "Slide Descriptions".
    assert slides_post["payload"]["parent"]["page_id"] == "page-1"
    assert slides_post["payload"]["properties"]["title"]["title"][0]["text"][
        "content"
    ] == np.SLIDES_SUBPAGE_TITLE


def test_publish_no_subpage_when_slides_absent(monkeypatch):
    import transcriber.backends.notion_publish as np

    monkeypatch.setenv("MY_NOTION_TOKEN", "ntn_secret")
    calls = _install_capturing_urlopen(monkeypatch)

    cfg = _make_config_for_publish()
    np.publish_to_notion(cfg, "My Meeting", "# Summary\n\nBody.", timeout=10.0)

    create_posts = [
        c for c in calls if c["method"] == "POST" and c["url"].endswith("/pages")
    ]
    # Only the summary page is created — no slides child page.
    assert len(create_posts) == 1


def test_publish_no_subpage_when_slides_whitespace(monkeypatch):
    import transcriber.backends.notion_publish as np

    monkeypatch.setenv("MY_NOTION_TOKEN", "ntn_secret")
    calls = _install_capturing_urlopen(monkeypatch)

    cfg = _make_config_for_publish()
    np.publish_to_notion(
        cfg, "My Meeting", "# Summary\n\nBody.", slides_markdown="   \n\t ",
        timeout=10.0,
    )
    create_posts = [
        c for c in calls if c["method"] == "POST" and c["url"].endswith("/pages")
    ]
    assert len(create_posts) == 1
