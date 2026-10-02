"""Tests for ``transcriber.publish.telegram`` (httpx fully mocked).

No real network calls are made: an ``httpx.MockTransport`` intercepts every
request and records the URL and JSON payload for assertions.
"""

from __future__ import annotations

import json

import httpx
import pytest

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
from transcriber.publish.telegram import (
    TELEGRAM_MAX_MESSAGE_CHARS,
    TelegramError,
    TelegramPublisher,
    chunk_message,
    resolve_chat_id,
    strip_markdown,
)

TOKEN_ENV = "TELEGRAM_BOT_TOKEN"
TOKEN_VALUE = "123456:ABC-fake-token"


def make_config(
    *,
    default_chat_id: str = "default-chat",
    routing: dict[str, str] | None = None,
    bot_token_env: str = TOKEN_ENV,
) -> Config:
    return Config(
        recordings_dir="./recordings",
        stages=Stages(slides=False, s3_sync=False),
        transcribe=Transcribe(model_id="scribe_v1"),
        summary=Summary(language="en", sections=[]),
        agent=Agent(cli="kiro", extra_args=[], output_file="{basename}.md"),
        notion=Notion(server="n", parent_page_id="p", insert="subpage"),
        telegram=Telegram(
            bot_token_env=bot_token_env,
            default_chat_id=default_chat_id,
            routing=routing or {},
        ),
        timeouts=Timeouts(),
        s3=None,
    )


class RecordingTransport(httpx.MockTransport):
    """MockTransport that records every intercepted request."""

    def __init__(self, status_code: int = 200) -> None:
        self.requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            return httpx.Response(
                status_code, json={"ok": status_code == 200, "result": {}}
            )

        super().__init__(handler)

    def payloads(self) -> list[dict]:
        return [json.loads(r.content) for r in self.requests]


@pytest.fixture(autouse=True)
def _set_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(TOKEN_ENV, TOKEN_VALUE)


def make_publisher(
    config: Config, transport: httpx.MockTransport
) -> TelegramPublisher:
    client = httpx.Client(transport=transport)
    return TelegramPublisher(config, client=client)


# --------------------------------------------------------------------------- #
# Routing
# --------------------------------------------------------------------------- #
def test_routing_matched_topic_uses_configured_chat():
    telegram = make_config(routing={"news": "chat-news"}).telegram
    assert resolve_chat_id(telegram, "news") == "chat-news"


def test_routing_unmatched_topic_falls_back_to_default():
    telegram = make_config(routing={"news": "chat-news"}).telegram
    assert resolve_chat_id(telegram, "sports") == "default-chat"


def test_routing_none_topic_falls_back_to_default():
    telegram = make_config(routing={"news": "chat-news"}).telegram
    assert resolve_chat_id(telegram, None) == "default-chat"


def test_routing_empty_topic_falls_back_to_default():
    telegram = make_config(routing={"news": "chat-news"}).telegram
    assert resolve_chat_id(telegram, "") == "default-chat"


def test_send_routes_matched_topic_payload():
    config = make_config(routing={"news": "chat-news"})
    transport = RecordingTransport()
    publisher = make_publisher(config, transport)

    publisher.send("hello world", topic="news")

    payloads = transport.payloads()
    assert len(payloads) == 1
    assert payloads[0]["chat_id"] == "chat-news"
    assert payloads[0]["text"] == "hello world"


def test_send_routes_unmatched_topic_to_default():
    config = make_config(routing={"news": "chat-news"})
    transport = RecordingTransport()
    publisher = make_publisher(config, transport)

    publisher.send("hello", topic="unknown-topic")

    payloads = transport.payloads()
    assert payloads[0]["chat_id"] == "default-chat"


# --------------------------------------------------------------------------- #
# Markdown stripping (sent with no parse_mode -> must be clean plain text)
# --------------------------------------------------------------------------- #
_BULLET = "\u2022"


def test_strip_markdown_removes_emphasis_and_code():
    assert (
        strip_markdown("Plain **bold** and _italic_ and `code`.")
        == "Plain bold and italic and code."
    )
    assert strip_markdown("__also bold__ and *also italic*") == "also bold and also italic"


def test_strip_markdown_removes_headings():
    assert strip_markdown("# Title") == "Title"
    assert strip_markdown("### Sub heading") == "Sub heading"


def test_strip_markdown_converts_bullets():
    assert strip_markdown("- item one") == f"{_BULLET} item one"
    assert strip_markdown("* item two") == f"{_BULLET} item two"
    assert strip_markdown("+ item three") == f"{_BULLET} item three"
    # Indentation preserved.
    assert strip_markdown("  - nested") == f"  {_BULLET} nested"


def test_strip_markdown_links_keep_target():
    assert (
        strip_markdown("See [the doc](https://notion.so/x) now.")
        == "See the doc (https://notion.so/x) now."
    )


def test_strip_markdown_images_keep_alt_and_url():
    assert strip_markdown("![alt](http://img/x.png)") == "alt (http://img/x.png)"


def test_strip_markdown_drops_code_fences_keeps_content():
    assert strip_markdown("```\ncode line\n```") == "code line"


def test_strip_markdown_blockquote():
    assert strip_markdown("> quoted") == "quoted"


def test_strip_markdown_preserves_line_structure():
    md = "# HIVE Weekly\n\n- **Dario:** follow up\n- *Alex:* draft"
    assert strip_markdown(md) == (
        f"HIVE Weekly\n\n{_BULLET} Dario: follow up\n{_BULLET} Alex: draft"
    )


def test_send_transmits_stripped_markdown():
    """send() must transmit clean plain text (no raw Markdown markers)."""
    config = make_config()
    transport = RecordingTransport()
    publisher = make_publisher(config, transport)

    publisher.send("# Title\n\n- **A:** do x\n- do y")

    payloads = transport.payloads()
    sent = payloads[0]["text"]
    assert "**" not in sent
    assert "# " not in sent
    assert sent.startswith("Title")
    assert f"{_BULLET} A: do x" in sent
    # No parse_mode is set (we send clean plain text).
    assert "parse_mode" not in payloads[0]


# --------------------------------------------------------------------------- #
# URL / token
# --------------------------------------------------------------------------- #
def test_send_url_contains_token():
    config = make_config()
    transport = RecordingTransport()
    publisher = make_publisher(config, transport)

    publisher.send("hi")

    url = str(transport.requests[0].url)
    assert url == f"https://api.telegram.org/bot{TOKEN_VALUE}/sendMessage"
    assert TOKEN_VALUE in url
    assert transport.requests[0].method == "POST"


def test_missing_token_env_raises_clear_error(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv(TOKEN_ENV, raising=False)
    config = make_config()
    transport = RecordingTransport()
    publisher = make_publisher(config, transport)

    with pytest.raises(TelegramError) as exc_info:
        publisher.send("hi")

    message = str(exc_info.value)
    assert TOKEN_ENV in message
    # No request should have been attempted.
    assert transport.requests == []


# --------------------------------------------------------------------------- #
# Chunking
# --------------------------------------------------------------------------- #
def test_chunk_message_short_single_chunk():
    assert chunk_message("short") == ["short"]


def test_chunk_message_empty_yields_single_empty_chunk():
    assert chunk_message("") == [""]


def test_chunk_message_exact_limit_single_chunk():
    text = "a" * TELEGRAM_MAX_MESSAGE_CHARS
    chunks = chunk_message(text)
    assert len(chunks) == 1
    assert chunks[0] == text


def test_chunk_message_over_limit_splits():
    text = "a" * (TELEGRAM_MAX_MESSAGE_CHARS + 1)
    chunks = chunk_message(text)
    assert len(chunks) == 2
    assert len(chunks[0]) == TELEGRAM_MAX_MESSAGE_CHARS
    assert len(chunks[1]) == 1
    assert "".join(chunks) == text


def test_chunk_message_multiple_splits_preserve_content():
    text = "x" * (TELEGRAM_MAX_MESSAGE_CHARS * 2 + 50)
    chunks = chunk_message(text)
    assert len(chunks) == 3
    assert all(len(c) <= TELEGRAM_MAX_MESSAGE_CHARS for c in chunks)
    assert "".join(chunks) == text


def test_send_long_message_makes_multiple_calls():
    config = make_config()
    transport = RecordingTransport()
    publisher = make_publisher(config, transport)

    text = "y" * (TELEGRAM_MAX_MESSAGE_CHARS + 100)
    responses = publisher.send(text, topic="news")

    payloads = transport.payloads()
    assert len(payloads) == 2
    assert len(responses) == 2
    # All chunks go to the same (default) chat.
    assert {p["chat_id"] for p in payloads} == {"default-chat"}
    # Concatenating the sent text reproduces the original.
    assert "".join(p["text"] for p in payloads) == text
    assert len(payloads[0]["text"]) == TELEGRAM_MAX_MESSAGE_CHARS
    assert len(payloads[1]["text"]) == 100


# --------------------------------------------------------------------------- #
# Error surfacing on HTTP failure
# --------------------------------------------------------------------------- #
def test_http_error_surfaces_as_telegram_error():
    config = make_config()
    transport = RecordingTransport(status_code=400)
    publisher = make_publisher(config, transport)

    with pytest.raises(TelegramError):
        publisher.send("boom", topic="news")


# --------------------------------------------------------------------------- #
# Demo: multi-topic summary
# --------------------------------------------------------------------------- #
def test_demo_multi_topic_summary():
    config = make_config(
        default_chat_id="chat-default",
        routing={"engineering": "chat-eng", "product": "chat-prod"},
    )
    transport = RecordingTransport()
    publisher = make_publisher(config, transport)

    publisher.send("Eng update", topic="engineering")
    publisher.send("Product update", topic="product")
    publisher.send("Misc note", topic="random")

    payloads = transport.payloads()
    assert [p["chat_id"] for p in payloads] == [
        "chat-eng",
        "chat-prod",
        "chat-default",
    ]
    assert [p["text"] for p in payloads] == [
        "Eng update",
        "Product update",
        "Misc note",
    ]
