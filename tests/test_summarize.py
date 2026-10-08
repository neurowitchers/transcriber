"""Tests for the summarize backend selection + the ``agno`` backend.

The ``agno`` backend no longer drives an Agno agent: it makes **direct
OpenRouter chat-completions calls** (reading ``choices[0].message.content``
straight from the response, which — unlike Agno's ``get_content_as_string()`` —
does not truncate reasoning-model output) and publishes the Notion subpage via
the REST API. These tests stub the direct OpenRouter seam
(:func:`transcriber.backends.summarize_agno._summarize_openrouter`) and the
engine-side Notion publish. No Agno import, no MCP subprocess, no network.

Verifies: the summarize_model + resolved key/base_url reach the OpenRouter call;
two calls are made (summary + digest); the engine publishes via the Notion REST
API; the non-empty ``<name>.md`` post-condition; and log-hygiene (key/token/
image bytes absent from logs).
"""

from __future__ import annotations

import logging

import pytest

from transcriber.backends.errors import SummarizeError
from transcriber.backends.interfaces import SummaryResult
from transcriber.backends.summarize import get_summarize_backend
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

OPENROUTER_KEY_ENV = "MY_OPENROUTER_KEY"
OPENROUTER_KEY_VALUE = "sk-or-secret-should-not-leak"
NOTION_TOKEN_ENV_NAME = "MY_NOTION_TOKEN"
NOTION_TOKEN_VALUE = "ntn_secret_should_not_leak"
IMAGE_BYTES_SENTINEL = "QUJDREVGR0hJSktMTU5PUA=="  # fake base64 "image bytes"


def make_config(
    *,
    backend: str = "agno",
    summarize_timeout: int | None = None,
    summary_model: str = "google/gemini-2.5-pro",
) -> Config:
    return Config(
        recordings_dir="./recordings",
        stages=Stages(slides=SlidesStage(enabled=False), s3_sync=False),
        transcribe=Transcribe(model_id="scribe_v1"),
        summary=Summary(
            language="en",
            sections=["Decisions", "Action Items"],
            backend=backend,
        ),
        agent=Agent(output_file="{basename}.md"),
        notion=Notion(
            server="notion-private",
            parent_page_id="PARENT-PAGE-ID-123",
            insert="subpage",
            token_env=NOTION_TOKEN_ENV_NAME,
        ),
        telegram=Telegram(
            bot_token_env="TG", default_chat_id="c", routing={}
        ),
        timeouts=Timeouts(summarize=summarize_timeout),
        openrouter=OpenRouter(
            api_key_env=OPENROUTER_KEY_ENV,
            base_url="https://openrouter.ai/api/v1",
            summary_model=summary_model,
        ),
    )


def write_transcript(tmp_path, text="[00:00] hello world"):
    p = tmp_path / "meeting.txt"
    p.write_text(text, encoding="utf-8")
    return p


# --------------------------------------------------------------------------- #
# Fake for the direct OpenRouter chat seam
# --------------------------------------------------------------------------- #
class FakeOpenRouter:
    """Records each ``_summarize_openrouter(config, prompt, timeout)`` call and
    returns a scripted reply.

    The backend makes two calls in order: summary, then digest. A third call
    (routing) is possible via :func:`build_routed_digests`. ``replies`` is a
    list consumed left-to-right; when exhausted, the last reply repeats.
    """

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls: list[tuple] = []

    def __call__(self, config, prompt, timeout):
        self.calls.append((config, prompt, timeout))
        idx = min(len(self.calls) - 1, len(self.replies) - 1)
        reply = self.replies[idx]
        if isinstance(reply, BaseException):
            raise reply
        return reply


def _patch_openrouter(monkeypatch, replies):
    """Patch the direct OpenRouter seam; return the recording fake."""
    import transcriber.backends.summarize_agno as sa

    fake = FakeOpenRouter(replies)
    monkeypatch.setattr(sa, "_summarize_openrouter", fake)
    return fake


class _RecordingPublish:
    """Stand-in for notion_publish.publish_to_notion; records calls."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.exc: Exception | None = None

    def __call__(self, config, title, summary_markdown, *, slides_markdown=None, timeout=None):
        self.calls.append((title, summary_markdown, slides_markdown, timeout))
        if self.exc is not None:
            raise self.exc
        return "https://app.notion.com/p/new-page-123"


def _patch_publish(monkeypatch) -> "_RecordingPublish":
    """Patch the engine-side Notion publish used by the agno backend."""
    import transcriber.backends.summarize_agno as sa

    rec = _RecordingPublish()
    monkeypatch.setattr(sa, "publish_to_notion", rec)
    return rec


# Default scripted replies: a full Markdown summary, then a short digest.
DEFAULT_SUMMARY = "# Summary\n\nBody."
DEFAULT_DIGEST = "Title\n- Decision"


def _backend():
    from transcriber.backends.summarize_agno import AgnoSummarizeBackend

    return AgnoSummarizeBackend()


# --------------------------------------------------------------------------- #
# Backend selection (pure function of config; no silent fallback)
# --------------------------------------------------------------------------- #
def test_get_summarize_backend_agno():
    cfg = make_config(backend="agno")
    backend = get_summarize_backend(cfg)
    assert type(backend).__name__ == "AgnoSummarizeBackend"


def test_get_summarize_backend_unknown_raises():
    cfg = make_config(backend="agno")
    cfg.summary.backend = "bogus"
    with pytest.raises(ValueError, match="unknown summary.backend"):
        get_summarize_backend(cfg)


# --------------------------------------------------------------------------- #
# AgnoSummarizeBackend — direct OpenRouter mocked, Notion publish mocked
# --------------------------------------------------------------------------- #
def test_agno_calls_openrouter_and_publishes_to_notion(tmp_path, monkeypatch):
    monkeypatch.setenv(OPENROUTER_KEY_ENV, OPENROUTER_KEY_VALUE)
    monkeypatch.setenv(NOTION_TOKEN_ENV_NAME, NOTION_TOKEN_VALUE)
    backend = _backend()
    fake = _patch_openrouter(monkeypatch, [DEFAULT_SUMMARY, DEFAULT_DIGEST])
    publish = _patch_publish(monkeypatch)

    cfg = make_config(backend="agno", summary_model="mistralai/mistral-medium-3.1")
    rec_dir = tmp_path / "rec"
    rec_dir.mkdir()
    tp = write_transcript(rec_dir)

    result = backend.summarize(tp, None, rec_dir, cfg)

    # Two OpenRouter calls (summary + digest), each with the SAME config object
    # that carries summary_model + key/base_url (resolved inside the helper).
    assert len(fake.calls) == 2
    for call_cfg, _prompt, _timeout in fake.calls:
        assert call_cfg.openrouter.summary_model == "mistralai/mistral-medium-3.1"
        assert call_cfg.openrouter.api_key_env == OPENROUTER_KEY_ENV
        assert call_cfg.openrouter.base_url == "https://openrouter.ai/api/v1"

    # Engine published once via the Notion REST helper, with the summary text
    # and the recording basename as the page title. No slides here -> no subpage.
    assert len(publish.calls) == 1
    title, summary_md, slides_md, _timeout = publish.calls[0]
    assert title == tp.stem
    assert "Body." in summary_md
    assert slides_md is None  # no slide descriptions supplied

    # Engine wrote the files from the plain-text summary + digest.
    assert isinstance(result, SummaryResult)
    assert "Body." in result.summary_path.read_text(encoding="utf-8")
    assert result.telegram_path.read_text(encoding="utf-8").strip() == (
        "Title\n- Decision"
    )


def test_agno_summarize_prompt_carries_slide_markdown(tmp_path, monkeypatch):
    monkeypatch.setenv(OPENROUTER_KEY_ENV, OPENROUTER_KEY_VALUE)
    monkeypatch.setenv(NOTION_TOKEN_ENV_NAME, NOTION_TOKEN_VALUE)
    backend = _backend()
    fake = _patch_openrouter(monkeypatch, [DEFAULT_SUMMARY, DEFAULT_DIGEST])
    _patch_publish(monkeypatch)

    cfg = make_config(backend="agno")
    rec_dir = tmp_path / "rec"
    rec_dir.mkdir()
    tp = write_transcript(rec_dir)
    slides_md = "### Slide 1\n**Timestamp:** 00:00 - 00:10\n\nIntro.\n"

    backend.summarize(tp, slides_md, rec_dir, cfg)

    # First (summary) call's prompt inlines transcript + slide markdown.
    first_prompt = fake.calls[0][1]
    assert slides_md in first_prompt


def test_agno_splits_slides_into_clean_file_and_notion_subpage(tmp_path, monkeypatch):
    """Split model: the summary file holds ONLY the summary; cleaned slide
    descriptions go to a separate <name>.slides-clean.md and to the Notion
    publish as slides_markdown (which creates a 'Slide Descriptions' subpage)."""
    monkeypatch.setenv(OPENROUTER_KEY_ENV, OPENROUTER_KEY_VALUE)
    monkeypatch.setenv(NOTION_TOKEN_ENV_NAME, NOTION_TOKEN_VALUE)
    backend = _backend()
    _patch_openrouter(monkeypatch, [DEFAULT_SUMMARY, DEFAULT_DIGEST])
    publish = _patch_publish(monkeypatch)

    cfg = make_config(backend="agno")
    rec_dir = tmp_path / "rec"
    rec_dir.mkdir()
    tp = write_transcript(rec_dir)
    # Includes a [[SLIDE_EMPTY]] marker that must be stripped from the clean output.
    slides_md = (
        "[[SLIDE_EMPTY 00:00 - 00:05]]\n\n"
        "### Slide 1\n**Timestamp:** 00:05 - 00:10\n\nIntro slide.\n"
    )

    result = backend.summarize(tp, slides_md, rec_dir, cfg)

    # Summary file holds ONLY the summary — no slides, no Slide Descriptions heading.
    file_text = result.summary_path.read_text(encoding="utf-8")
    assert "Body." in file_text
    assert "## Slide Descriptions" not in file_text
    assert "Slide 1" not in file_text

    # Cleaned slides live in <name>.slides-clean.md (markers stripped).
    clean_path = result.summary_path.with_suffix(".slides-clean.md")
    assert clean_path.exists()
    clean_text = clean_path.read_text(encoding="utf-8")
    assert "### Slide 1" in clean_text
    assert "Intro slide." in clean_text
    assert "SLIDE_EMPTY" not in clean_text

    # Notion publish got the summary + the cleaned slides (as the subpage source).
    assert len(publish.calls) == 1
    _title, published_summary, published_slides, _timeout = publish.calls[0]
    assert "Body." in published_summary
    assert "## Slide Descriptions" not in published_summary  # subpage, not inline
    assert published_slides is not None
    assert "### Slide 1" in published_slides
    assert "SLIDE_EMPTY" not in published_slides

    # The digest stays slide-free.
    assert "Slide" not in result.telegram_path.read_text(encoding="utf-8")


def test_agno_no_slide_section_when_slides_absent(tmp_path, monkeypatch):
    monkeypatch.setenv(OPENROUTER_KEY_ENV, OPENROUTER_KEY_VALUE)
    monkeypatch.setenv(NOTION_TOKEN_ENV_NAME, NOTION_TOKEN_VALUE)
    backend = _backend()
    _patch_openrouter(monkeypatch, [DEFAULT_SUMMARY, DEFAULT_DIGEST])
    publish = _patch_publish(monkeypatch)

    cfg = make_config(backend="agno")
    rec_dir = tmp_path / "rec"
    rec_dir.mkdir()
    tp = write_transcript(rec_dir)

    result = backend.summarize(tp, None, rec_dir, cfg)
    assert "## Slide Descriptions" not in result.summary_path.read_text(
        encoding="utf-8"
    )
    # No cleaned-slides file created when there are no slides.
    assert not result.summary_path.with_suffix(".slides-clean.md").exists()
    _title, published_summary, published_slides, _timeout = publish.calls[0]
    assert "## Slide Descriptions" not in published_summary
    assert published_slides is None  # no slides subpage


def test_agno_empty_summary_fails_stage(tmp_path, monkeypatch):
    monkeypatch.setenv(OPENROUTER_KEY_ENV, OPENROUTER_KEY_VALUE)
    monkeypatch.setenv(NOTION_TOKEN_ENV_NAME, NOTION_TOKEN_VALUE)
    backend = _backend()
    # Summary comes back whitespace-only -> fail before publishing.
    _patch_openrouter(monkeypatch, ["   ", DEFAULT_DIGEST])
    publish = _patch_publish(monkeypatch)

    cfg = make_config(backend="agno")
    rec_dir = tmp_path / "rec"
    rec_dir.mkdir()
    tp = write_transcript(rec_dir)

    with pytest.raises(SummarizeError, match="no usable summary"):
        backend.summarize(tp, None, rec_dir, cfg)
    # Never published (bailed before publish).
    assert publish.calls == []


def test_agno_openrouter_failure_fails_stage(tmp_path, monkeypatch):
    """A SummarizeError from the OpenRouter call surfaces as a stage failure."""
    monkeypatch.setenv(OPENROUTER_KEY_ENV, OPENROUTER_KEY_VALUE)
    monkeypatch.setenv(NOTION_TOKEN_ENV_NAME, NOTION_TOKEN_VALUE)
    backend = _backend()
    _patch_openrouter(
        monkeypatch,
        [SummarizeError("openrouter call failed: status 400: bad model")],
    )
    publish = _patch_publish(monkeypatch)

    cfg = make_config(backend="agno")
    rec_dir = tmp_path / "rec"
    rec_dir.mkdir()
    tp = write_transcript(rec_dir)

    with pytest.raises(SummarizeError, match="openrouter call failed"):
        backend.summarize(tp, None, rec_dir, cfg)
    assert publish.calls == []


def test_agno_notion_publish_failure_fails_stage(tmp_path, monkeypatch):
    monkeypatch.setenv(OPENROUTER_KEY_ENV, OPENROUTER_KEY_VALUE)
    monkeypatch.setenv(NOTION_TOKEN_ENV_NAME, NOTION_TOKEN_VALUE)
    backend = _backend()
    _patch_openrouter(monkeypatch, [DEFAULT_SUMMARY, DEFAULT_DIGEST])
    publish = _patch_publish(monkeypatch)
    publish.exc = SummarizeError("Notion API create page failed: HTTP 400 boom")

    cfg = make_config(backend="agno")
    rec_dir = tmp_path / "rec"
    rec_dir.mkdir()
    tp = write_transcript(rec_dir)

    with pytest.raises(SummarizeError, match="Notion API"):
        backend.summarize(tp, None, rec_dir, cfg)


def test_agno_persists_summary_before_publish(tmp_path, monkeypatch):
    """Local artifacts are written BEFORE publishing.

    A publish failure must still leave the summary file on disk, so a retry
    regenerates nothing (and — with the publish record — never duplicates the
    Notion page).
    """
    monkeypatch.setenv(OPENROUTER_KEY_ENV, OPENROUTER_KEY_VALUE)
    monkeypatch.setenv(NOTION_TOKEN_ENV_NAME, NOTION_TOKEN_VALUE)
    backend = _backend()
    _patch_openrouter(monkeypatch, [DEFAULT_SUMMARY, DEFAULT_DIGEST])
    publish = _patch_publish(monkeypatch)
    publish.exc = SummarizeError("Notion API create page failed: HTTP 500 boom")

    cfg = make_config(backend="agno")
    rec_dir = tmp_path / "rec"
    rec_dir.mkdir()
    tp = write_transcript(rec_dir)

    with pytest.raises(SummarizeError):
        backend.summarize(tp, None, rec_dir, cfg)

    # Summary file is present despite the publish failure.
    summary_file = rec_dir / "meeting.md"
    assert summary_file.exists()
    assert "Body." in summary_file.read_text(encoding="utf-8")
    # No publish record: the publish did not succeed, so a retry WILL publish.
    assert not (rec_dir / "meeting.notion_published.json").exists()


def test_agno_publish_is_idempotent_via_record(tmp_path, monkeypatch):
    """A prior publish record prevents a duplicate page.

    Simulates a crash after a successful publish but before the manifest was
    marked: on retry the publish record already exists, so the engine skips the
    publish (no duplicate Notion page) while still refreshing local files.
    """
    monkeypatch.setenv(OPENROUTER_KEY_ENV, OPENROUTER_KEY_VALUE)
    monkeypatch.setenv(NOTION_TOKEN_ENV_NAME, NOTION_TOKEN_VALUE)
    backend = _backend()
    # Enough replies for two runs (summary+digest each); last repeats.
    _patch_openrouter(monkeypatch, [DEFAULT_SUMMARY, DEFAULT_DIGEST])
    publish = _patch_publish(monkeypatch)

    cfg = make_config(backend="agno")
    rec_dir = tmp_path / "rec"
    rec_dir.mkdir()
    tp = write_transcript(rec_dir)

    # First run publishes once and drops a publish record.
    backend.summarize(tp, None, rec_dir, cfg)
    assert len(publish.calls) == 1
    record = rec_dir / "meeting.notion_published.json"
    assert record.exists()

    # Second run (retry): publish is SKIPPED because the record exists.
    backend.summarize(tp, None, rec_dir, cfg)
    assert len(publish.calls) == 1  # still only the first publish


def test_agno_empty_digest_clears_stale_telegram_file(tmp_path, monkeypatch):
    """An empty digest removes any stale digest file."""
    monkeypatch.setenv(OPENROUTER_KEY_ENV, OPENROUTER_KEY_VALUE)
    monkeypatch.setenv(NOTION_TOKEN_ENV_NAME, NOTION_TOKEN_VALUE)
    backend = _backend()
    # Summary ok, digest empty.
    _patch_openrouter(monkeypatch, [DEFAULT_SUMMARY, "   "])
    _patch_publish(monkeypatch)

    # A stale digest from an earlier run.
    rec_dir = tmp_path / "rec"
    rec_dir.mkdir()
    stale = rec_dir / "meeting.telegram.md"
    stale.write_text("OLD STALE DIGEST\n", encoding="utf-8")

    tp = write_transcript(rec_dir)
    cfg = make_config(backend="agno")

    result = backend.summarize(tp, None, rec_dir, cfg)

    # The stale digest is removed rather than left to be re-disseminated.
    assert not stale.exists()
    assert result.summary_path.read_text(encoding="utf-8").strip() != ""


def test_agno_timeout_fails_stage(tmp_path, monkeypatch):
    monkeypatch.setenv(OPENROUTER_KEY_ENV, OPENROUTER_KEY_VALUE)
    monkeypatch.setenv(NOTION_TOKEN_ENV_NAME, NOTION_TOKEN_VALUE)
    backend = _backend()
    _patch_publish(monkeypatch)

    import time as _time
    import transcriber.backends.summarize_agno as sa

    monkeypatch.setattr(sa, "_summarize_timeout", lambda config: 0.05)

    def slow_call(config, prompt, timeout):
        _time.sleep(5)  # exceeds the 0.05s bound -> TimeoutError
        return DEFAULT_SUMMARY

    monkeypatch.setattr(sa, "_summarize_openrouter", slow_call)

    cfg = make_config(backend="agno")
    rec_dir = tmp_path / "rec"
    rec_dir.mkdir()
    tp = write_transcript(rec_dir)

    with pytest.raises(SummarizeError, match="timed out"):
        backend.summarize(tp, None, rec_dir, cfg)


def test_agno_uses_summarize_timeout_when_set():
    from transcriber.backends.summarize_agno import _summarize_timeout

    cfg = make_config(backend="agno", summarize_timeout=123)
    assert _summarize_timeout(cfg) == 123.0


def test_agno_falls_back_to_default_timeout():
    from transcriber.backends.summarize_agno import _summarize_timeout
    from transcriber.config import DEFAULT_TIMEOUT_SECONDS

    cfg = make_config(backend="agno", summarize_timeout=None)
    # Unset summarize timeout falls back to the default (900s).
    assert _summarize_timeout(cfg) == float(DEFAULT_TIMEOUT_SECONDS)


# --------------------------------------------------------------------------- #
# Log hygiene (E8): key / token / image bytes never logged
# --------------------------------------------------------------------------- #
def test_agno_log_hygiene_no_secrets(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv(OPENROUTER_KEY_ENV, OPENROUTER_KEY_VALUE)
    monkeypatch.setenv(NOTION_TOKEN_ENV_NAME, NOTION_TOKEN_VALUE)
    backend = _backend()
    _patch_openrouter(monkeypatch, [DEFAULT_SUMMARY, DEFAULT_DIGEST])
    _patch_publish(monkeypatch)

    cfg = make_config(backend="agno")
    rec_dir = tmp_path / "rec"
    rec_dir.mkdir()
    tp = write_transcript(rec_dir)

    with caplog.at_level(logging.DEBUG):
        backend.summarize(tp, None, rec_dir, cfg)

    text = "\n".join(r.getMessage() for r in caplog.records)
    assert OPENROUTER_KEY_VALUE not in text
    assert NOTION_TOKEN_VALUE not in text
    assert IMAGE_BYTES_SENTINEL not in text
