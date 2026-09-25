"""Tests for transcriber.backends: OpenRouter vision helper + error/interface
surface.

``httpx`` is mocked at the ``httpx.post`` seam; ``time.sleep`` is patched so
retry tests run instantly. No real network calls are made.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

import pytest

import transcriber.backends.openrouter as orv
from transcriber.backends import (
    SlideDescribeError,
    SlideInput,
    SlidesBackend,
    SummarizeBackend,
    SummarizeError,
    SummaryResult,
    _openrouter_vision,
)
from transcriber.config import (
    Agent,
    Config,
    Notion,
    OpenRouter,
    S3,
    SlidesStage,
    Stages,
    Summary,
    Telegram,
    Timeouts,
    Transcribe,
)

API_KEY = "sk-or-secret-key-DO-NOT-LOG"
API_KEY_ENV = "OPENROUTER_API_KEY"
MODEL = "google/gemini-2.0-flash-001"
BASE_URL = "https://openrouter.example/api/v1"


# --------------------------------------------------------------------------- #
# Fixtures / helpers
# --------------------------------------------------------------------------- #
def make_config(base_url: str = BASE_URL) -> Config:
    return Config(
        recordings_dir="./recordings",
        stages=Stages(slides=SlidesStage(enabled=True, backend="openrouter"),
                      s3_sync=False),
        transcribe=Transcribe(model_id="scribe_v1"),
        summary=Summary(language="en", sections=["overview"]),
        agent=Agent(cli="agy", extra_args=[], output_file="{basename}.md"),
        notion=Notion(server="notion", parent_page_id="P", insert="subpage"),
        telegram=Telegram(
            bot_token_env="TELEGRAM_BOT_TOKEN", default_chat_id="1", routing={}
        ),
        timeouts=Timeouts(),
        openrouter=OpenRouter(
            api_key_env=API_KEY_ENV,
            base_url=base_url,
            slides_model=MODEL,
        ),
    )


class FakeResponse:
    """Minimal stand-in for ``httpx.Response``."""

    def __init__(
        self, status_code: int, *, json_data: Any = None, text: str = "",
        raise_on_json: bool = False,
    ) -> None:
        self.status_code = status_code
        self._json_data = json_data
        self.text = text
        self._raise_on_json = raise_on_json

    def json(self) -> Any:
        if self._raise_on_json:
            raise ValueError("No JSON could be decoded")
        return self._json_data


def ok_body(content: str = "## Slide 1\n**Timestamp:** 00:00 - 00:10") -> dict:
    return {"choices": [{"message": {"content": content}}]}


@pytest.fixture(autouse=True)
def _set_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(API_KEY_ENV, API_KEY)


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    # Patch the sleep used for backoff so retry tests are instant.
    monkeypatch.setattr(orv.time, "sleep", lambda _s: None)


def install_post(monkeypatch: pytest.MonkeyPatch, responses):
    """Install a fake ``httpx.post`` returning queued responses in order.

    ``responses`` is a list of FakeResponse (or Exception instances to raise).
    Records each call's (url, headers, json) into ``calls``.
    """
    calls: list[dict[str, Any]] = []
    queue = list(responses)

    def fake_post(url, *, headers=None, json=None, timeout=None):
        calls.append(
            {"url": url, "headers": headers, "json": json, "timeout": timeout}
        )
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(orv.httpx, "post", fake_post)
    return calls


MESSAGES = [{"role": "user", "content": [{"type": "text", "text": "hi"}]}]


# --------------------------------------------------------------------------- #
# Success: headers, URL, model
# --------------------------------------------------------------------------- #
def test_success_sends_expected_headers_url_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = install_post(monkeypatch, [FakeResponse(200, json_data=ok_body("OK"))])
    config = make_config()

    result = _openrouter_vision(config, MESSAGES, MODEL, timeout=30)

    assert result == "OK"
    assert len(calls) == 1
    call = calls[0]
    assert call["url"] == f"{BASE_URL}/chat/completions"
    assert call["headers"]["Authorization"] == f"Bearer {API_KEY}"
    assert call["headers"]["HTTP-Referer"]
    assert call["headers"]["X-Title"]
    assert call["json"]["model"] == MODEL
    assert call["json"]["messages"] == MESSAGES
    assert call["timeout"] == 30


def test_empty_content_returns_empty_string(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_post(monkeypatch, [FakeResponse(200, json_data=ok_body(""))])
    assert _openrouter_vision(make_config(), MESSAGES, MODEL, timeout=1) == ""


# --------------------------------------------------------------------------- #
# Retry: 429 -> 200, persistent 5xx
# --------------------------------------------------------------------------- #
def test_retry_429_then_200(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = install_post(
        monkeypatch,
        [
            FakeResponse(429, text="rate limited"),
            FakeResponse(200, json_data=ok_body("recovered")),
        ],
    )
    result = _openrouter_vision(make_config(), MESSAGES, MODEL, timeout=1)
    assert result == "recovered"
    assert len(calls) == 2


def test_persistent_5xx_raises_after_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = install_post(
        monkeypatch,
        [FakeResponse(503, text="unavailable")] * orv.MAX_ATTEMPTS,
    )
    with pytest.raises(SlideDescribeError) as excinfo:
        _openrouter_vision(make_config(), MESSAGES, MODEL, timeout=1)
    assert len(calls) == orv.MAX_ATTEMPTS
    assert "503" in str(excinfo.value)


def test_transport_error_retried_then_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import httpx

    calls = install_post(
        monkeypatch,
        [httpx.ConnectError("boom")] * orv.MAX_ATTEMPTS,
    )
    with pytest.raises(SlideDescribeError):
        _openrouter_vision(make_config(), MESSAGES, MODEL, timeout=1)
    assert len(calls) == orv.MAX_ATTEMPTS


def test_transport_error_then_success(monkeypatch: pytest.MonkeyPatch) -> None:
    import httpx

    install_post(
        monkeypatch,
        [httpx.ConnectError("boom"), FakeResponse(200, json_data=ok_body("ok"))],
    )
    assert _openrouter_vision(make_config(), MESSAGES, MODEL, timeout=1) == "ok"


# --------------------------------------------------------------------------- #
# Non-2xx (non-retryable), malformed JSON, missing content
# --------------------------------------------------------------------------- #
def test_non_retryable_4xx_raises_immediately(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = install_post(monkeypatch, [FakeResponse(400, text="bad request")])
    with pytest.raises(SlideDescribeError) as excinfo:
        _openrouter_vision(make_config(), MESSAGES, MODEL, timeout=1)
    assert len(calls) == 1
    assert "400" in str(excinfo.value)


def test_malformed_json_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    install_post(
        monkeypatch,
        [FakeResponse(200, raise_on_json=True, text="<html>not json</html>")],
    )
    with pytest.raises(SlideDescribeError) as excinfo:
        _openrouter_vision(make_config(), MESSAGES, MODEL, timeout=1)
    assert "malformed JSON" in str(excinfo.value)


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"choices": []},
        {"choices": [{}]},
        {"choices": [{"message": {}}]},
        {"choices": [{"message": {"content": None}}]},
        {"choices": "nope"},
    ],
)
def test_missing_content_raises(
    monkeypatch: pytest.MonkeyPatch, body: dict
) -> None:
    install_post(monkeypatch, [FakeResponse(200, json_data=body)])
    with pytest.raises(SlideDescribeError):
        _openrouter_vision(make_config(), MESSAGES, MODEL, timeout=1)


def test_missing_openrouter_config_raises() -> None:
    config = make_config()
    object.__setattr__(config, "openrouter", None)
    with pytest.raises(SlideDescribeError):
        _openrouter_vision(config, MESSAGES, MODEL, timeout=1)


# --------------------------------------------------------------------------- #
# Log hygiene: no secrets / bytes in errors or logs
# --------------------------------------------------------------------------- #
IMAGE_BYTES = "data:image/jpeg;base64," + ("A" * 500)


def test_error_does_not_leak_our_key_or_sent_bytes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The helper must never concatenate our own API key, our request headers,
    # or the base64 image bytes we *send* into the raised error. The provider
    # body here is a plain error (no echoed secret).
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "context"},
                {"type": "image_url", "image_url": {"url": IMAGE_BYTES}},
            ],
        }
    ]
    install_post(
        monkeypatch,
        [FakeResponse(500, text="internal error")] * orv.MAX_ATTEMPTS,
    )
    with pytest.raises(SlideDescribeError) as excinfo:
        _openrouter_vision(make_config(), messages, MODEL, timeout=1)
    msg = str(excinfo.value)
    assert API_KEY not in msg
    assert f"Bearer {API_KEY}" not in msg
    assert IMAGE_BYTES not in msg
    assert "500" in msg


def test_error_body_snippet_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    # A huge provider body (even one echoing a base64 blob) is truncated to a
    # bounded snippet, so it can never dump full image bytes into the error.
    body = "x" * 5000 + IMAGE_BYTES
    install_post(monkeypatch, [FakeResponse(400, text=body)])
    with pytest.raises(SlideDescribeError) as excinfo:
        _openrouter_vision(make_config(), MESSAGES, MODEL, timeout=1)
    msg = str(excinfo.value)
    assert IMAGE_BYTES not in msg
    assert len(msg) < 400  # status + bounded snippet only


def test_logs_have_no_secrets_or_bytes(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "context"},
                {"type": "image_url", "image_url": {"url": IMAGE_BYTES}},
            ],
        }
    ]
    install_post(
        monkeypatch,
        [FakeResponse(503, text="temporarily down"),
         FakeResponse(200, json_data=ok_body("done"))],
    )
    with caplog.at_level(logging.DEBUG, logger=orv.logger.name):
        result = _openrouter_vision(make_config(), messages, MODEL, timeout=1)
    assert result == "done"
    combined = "\n".join(r.getMessage() for r in caplog.records)
    assert API_KEY not in combined
    assert IMAGE_BYTES not in combined
    # And the Authorization header value never appears in logs.
    assert f"Bearer {API_KEY}" not in combined


# --------------------------------------------------------------------------- #
# Interface / error-type surface
# --------------------------------------------------------------------------- #
def test_error_types_exported_and_distinct() -> None:
    assert issubclass(SlideDescribeError, Exception)
    assert issubclass(SummarizeError, Exception)
    assert SlideDescribeError is not SummarizeError


def test_slide_input_and_summary_result_shapes(tmp_path) -> None:
    si = SlideInput(image_path=tmp_path / "a.jpg", timestamp="00:00 - 00:10")
    assert si.timestamp == "00:00 - 00:10"
    sr = SummaryResult(
        summary_path=tmp_path / "m.md", telegram_path=tmp_path / "m.telegram.md"
    )
    assert sr.summary_path.name == "m.md"


def test_protocols_are_runtime_checkable() -> None:
    class _Slides:
        def describe(self, slides, transcript_text, config, *, timeout):
            return ""

    class _Summarize:
        def summarize(self, transcript_path, slides_markdown, recording_dir, config):
            return None

    assert isinstance(_Slides(), SlidesBackend)
    assert isinstance(_Summarize(), SummarizeBackend)
