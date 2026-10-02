"""Tests for the summarize backends (Task 4).

* ``AgySummarizeBackend`` wraps ``transcriber.agent.run_agent`` (agy mocked).
* ``AgnoSummarizeBackend`` drives an Agno agent + Notion MCP, all mocked. ``agno``
  is an optional extra and is not installed here, so fake ``agno.agent`` /
  ``agno.models.openrouter`` / ``agno.tools.mcp`` modules are injected into
  ``sys.modules`` before the lazy imports run. No MCP subprocess or network.

Verifies: model built with ``summary_model`` + resolved key/base_url; Notion MCP
attached with the token resolved by env-var name; shared prompt run; non-empty
``<name>.md`` post-condition; ``MCPTools`` closed on the exception/timeout path;
log-hygiene (key/token/image bytes absent from logs).
"""

from __future__ import annotations

import asyncio
import logging
import sys
import types

import pytest

from transcriber.backends.errors import SummarizeError
from transcriber.backends.interfaces import SummaryResult
from transcriber.backends.summarize import (
    AgySummarizeBackend,
    get_summarize_backend,
)
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
        agent=Agent(cli="agy", extra_args=[], output_file="{basename}.md"),
        notion=Notion(
            server="notion-private",
            parent_page_id="PARENT-PAGE-ID-123",
            insert="subpage",
            token_env=NOTION_TOKEN_ENV_NAME,
        ),
        telegram=Telegram(
            bot_token_env="TG", default_chat_id="c", routing={}
        ),
        timeouts=Timeouts(agy=42, summarize=summarize_timeout),
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
# Fakes for Agno + MCPTools
# --------------------------------------------------------------------------- #
class FakeOpenRouterModel:
    instances: list["FakeOpenRouterModel"] = []

    def __init__(self, *, id, api_key, base_url) -> None:  # noqa: A002
        self.id = id
        self.api_key = api_key
        self.base_url = base_url
        FakeOpenRouterModel.instances.append(self)


class FakeRunOutput:
    def __init__(self, content) -> None:
        self.content = content

    def get_content_as_string(self) -> str:
        return self.content if isinstance(self.content, str) else ""


class FakeAgent:
    """Stand-in for agno's ``Agent`` — the agno path now makes two plain calls.

    First ``arun`` returns the Markdown summary, second returns the digest.
    """

    instances: list["FakeAgent"] = []

    summary_text = "# Summary\n\nBody."
    digest_text = "Title\n- Decision"
    _plain_calls = 0

    def __init__(self, *, model, tools=None, output_schema=None) -> None:
        self.model = model
        self.tools = tools or []
        self.output_schema = output_schema
        self.arun_called_with = None
        FakeAgent.instances.append(self)

    async def arun(self, prompt: str):
        self.arun_called_with = prompt
        FakeAgent._plain_calls += 1
        text = (
            FakeAgent.summary_text
            if FakeAgent._plain_calls == 1
            else FakeAgent.digest_text
        )
        return FakeRunOutput(text)



@pytest.fixture(autouse=True)
def _reset_fakes():
    FakeOpenRouterModel.instances = []
    FakeAgent.instances = []
    FakeAgent.summary_text = "# Summary\n\nBody."
    FakeAgent.digest_text = "Title\n- Decision"
    FakeAgent._plain_calls = 0
    yield
    FakeAgent._plain_calls = 0


def _install_fake_agno(monkeypatch) -> None:
    agno_mod = types.ModuleType("agno")
    agent_mod = types.ModuleType("agno.agent")
    agent_mod.Agent = FakeAgent  # type: ignore[attr-defined]
    models_mod = types.ModuleType("agno.models")
    openrouter_mod = types.ModuleType("agno.models.openrouter")
    openrouter_mod.OpenRouter = FakeOpenRouterModel  # type: ignore[attr-defined]

    for name, mod in {
        "agno": agno_mod,
        "agno.agent": agent_mod,
        "agno.models": models_mod,
        "agno.models.openrouter": openrouter_mod,
    }.items():
        monkeypatch.setitem(sys.modules, name, mod)


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


# --------------------------------------------------------------------------- #
# Backend selection (pure function of config; no silent fallback)
# --------------------------------------------------------------------------- #
def test_get_summarize_backend_defaults_to_agy():
    cfg = make_config(backend="agy")
    backend = get_summarize_backend(cfg)
    assert isinstance(backend, AgySummarizeBackend)


def test_get_summarize_backend_agno(monkeypatch):
    _install_fake_agno(monkeypatch)
    cfg = make_config(backend="agno")
    backend = get_summarize_backend(cfg)
    # Imported lazily; class name is AgnoSummarizeBackend.
    assert type(backend).__name__ == "AgnoSummarizeBackend"


def test_get_summarize_backend_unknown_raises():
    cfg = make_config(backend="agy")
    cfg.summary.backend = "bogus"
    with pytest.raises(ValueError, match="unknown summary.backend"):
        get_summarize_backend(cfg)


# --------------------------------------------------------------------------- #
# AgySummarizeBackend — wraps run_agent (agy behavior unchanged)
# --------------------------------------------------------------------------- #
def test_agy_summarize_writes_and_returns_paths(tmp_path, monkeypatch):
    import transcriber.agent as agent

    cfg = make_config(backend="agy")
    rec_dir = tmp_path / "rec"
    rec_dir.mkdir()
    tp = write_transcript(rec_dir)

    def fake_run(prompt, *, add_dirs, extra_args, timeout, **kwargs):
        (rec_dir / "meeting.md").write_text("# Summary\n", encoding="utf-8")
        return ""

    monkeypatch.setattr(agent, "run", fake_run)

    result = AgySummarizeBackend().summarize(tp, None, rec_dir, cfg)
    assert isinstance(result, SummaryResult)
    assert result.summary_path == (rec_dir / "meeting.md").resolve()
    assert result.telegram_path.name == "meeting.telegram.md"
    assert result.summary_path.read_text(encoding="utf-8").strip()


def test_agy_summarize_threads_slides_markdown(tmp_path, monkeypatch):
    import transcriber.agent as agent

    cfg = make_config(backend="agy")
    rec_dir = tmp_path / "rec"
    rec_dir.mkdir()
    tp = write_transcript(rec_dir)
    slides_md = "### Slide 1\n**Timestamp:** 00:00 - 00:10\n\nIntro slide.\n"

    captured = {}

    def fake_run(prompt, *, add_dirs, extra_args, timeout, **kwargs):
        captured["prompt"] = prompt
        (rec_dir / "meeting.md").write_text("# Summary\n", encoding="utf-8")
        return ""

    monkeypatch.setattr(agent, "run", fake_run)
    AgySummarizeBackend().summarize(tp, slides_md, rec_dir, cfg)
    assert slides_md in captured["prompt"]


# --------------------------------------------------------------------------- #
# AgnoSummarizeBackend — Agno model mocked, engine-side Notion publish mocked
# --------------------------------------------------------------------------- #
def _agno_backend(monkeypatch):
    _install_fake_agno(monkeypatch)
    from transcriber.backends.summarize_agno import AgnoSummarizeBackend

    return AgnoSummarizeBackend()


def _force_missing_agno(monkeypatch) -> None:
    for name in (
        "agno",
        "agno.agent",
        "agno.models",
        "agno.models.openrouter",
    ):
        monkeypatch.setitem(sys.modules, name, None)


def test_agno_builds_model_and_publishes_to_notion(tmp_path, monkeypatch):
    monkeypatch.setenv(OPENROUTER_KEY_ENV, OPENROUTER_KEY_VALUE)
    monkeypatch.setenv(NOTION_TOKEN_ENV_NAME, NOTION_TOKEN_VALUE)
    backend = _agno_backend(monkeypatch)
    publish = _patch_publish(monkeypatch)

    cfg = make_config(backend="agno", summary_model="mistralai/mistral-medium-3.1")
    rec_dir = tmp_path / "rec"
    rec_dir.mkdir()
    tp = write_transcript(rec_dir)

    result = backend.summarize(tp, None, rec_dir, cfg)

    # Two model instances built (summary + digest), both with the configured
    # summary_model + resolved key/base_url.
    assert len(FakeOpenRouterModel.instances) == 2
    for model in FakeOpenRouterModel.instances:
        assert model.id == "mistralai/mistral-medium-3.1"
        assert model.api_key == OPENROUTER_KEY_VALUE
        assert model.base_url == "https://openrouter.ai/api/v1"

    # Two agents: summary + digest (no publish agent — engine publishes).
    assert len(FakeAgent.instances) == 2

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
    backend = _agno_backend(monkeypatch)
    _patch_publish(monkeypatch)

    cfg = make_config(backend="agno")
    rec_dir = tmp_path / "rec"
    rec_dir.mkdir()
    tp = write_transcript(rec_dir)
    slides_md = "### Slide 1\n**Timestamp:** 00:00 - 00:10\n\nIntro.\n"

    backend.summarize(tp, slides_md, rec_dir, cfg)

    # First (summary) call inlines transcript + slide markdown.
    assert slides_md in FakeAgent.instances[0].arun_called_with


def test_agno_splits_slides_into_clean_file_and_notion_subpage(tmp_path, monkeypatch):
    """Split model: the summary file holds ONLY the summary; cleaned slide
    descriptions go to a separate <name>.slides-clean.md and to the Notion
    publish as slides_markdown (which creates a 'Slide Descriptions' subpage)."""
    monkeypatch.setenv(OPENROUTER_KEY_ENV, OPENROUTER_KEY_VALUE)
    monkeypatch.setenv(NOTION_TOKEN_ENV_NAME, NOTION_TOKEN_VALUE)
    backend = _agno_backend(monkeypatch)
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
    backend = _agno_backend(monkeypatch)
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
    backend = _agno_backend(monkeypatch)
    publish = _patch_publish(monkeypatch)

    cfg = make_config(backend="agno")
    rec_dir = tmp_path / "rec"
    rec_dir.mkdir()
    tp = write_transcript(rec_dir)

    # Summary comes back whitespace-only -> fail before publishing.
    FakeAgent.summary_text = "   "
    with pytest.raises(SummarizeError, match="no usable summary"):
        backend.summarize(tp, None, rec_dir, cfg)
    # Never published (bailed before publish).
    assert publish.calls == []


def test_agno_notion_publish_failure_fails_stage(tmp_path, monkeypatch):
    monkeypatch.setenv(OPENROUTER_KEY_ENV, OPENROUTER_KEY_VALUE)
    monkeypatch.setenv(NOTION_TOKEN_ENV_NAME, NOTION_TOKEN_VALUE)
    backend = _agno_backend(monkeypatch)
    publish = _patch_publish(monkeypatch)
    publish.exc = SummarizeError("Notion API create page failed: HTTP 400 boom")

    cfg = make_config(backend="agno")
    rec_dir = tmp_path / "rec"
    rec_dir.mkdir()
    tp = write_transcript(rec_dir)

    with pytest.raises(SummarizeError, match="Notion API"):
        backend.summarize(tp, None, rec_dir, cfg)


def test_agno_persists_summary_before_publish(tmp_path, monkeypatch):
    """Regression (Copilot #7): local artifacts are written BEFORE publishing.

    A publish failure must still leave the summary file on disk, so a retry
    regenerates nothing (and — with the publish record — never duplicates the
    Notion page).
    """
    monkeypatch.setenv(OPENROUTER_KEY_ENV, OPENROUTER_KEY_VALUE)
    monkeypatch.setenv(NOTION_TOKEN_ENV_NAME, NOTION_TOKEN_VALUE)
    backend = _agno_backend(monkeypatch)
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
    """Regression (Copilot #7): a prior publish record prevents a duplicate page.

    Simulates a crash after a successful publish but before the manifest was
    marked: on retry the publish record already exists, so the engine skips the
    publish (no duplicate Notion page) while still refreshing local files.
    """
    monkeypatch.setenv(OPENROUTER_KEY_ENV, OPENROUTER_KEY_VALUE)
    monkeypatch.setenv(NOTION_TOKEN_ENV_NAME, NOTION_TOKEN_VALUE)
    backend = _agno_backend(monkeypatch)
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

    # Reset the model fakes for a clean second run (retry).
    FakeAgent.instances = []
    FakeAgent._plain_calls = 0
    FakeOpenRouterModel.instances = []

    # Second run (retry): publish is SKIPPED because the record exists.
    backend.summarize(tp, None, rec_dir, cfg)
    assert len(publish.calls) == 1  # still only the first publish


def test_agno_empty_digest_clears_stale_telegram_file(tmp_path, monkeypatch):
    """Regression (Copilot #8): an empty digest removes any stale digest file."""
    monkeypatch.setenv(OPENROUTER_KEY_ENV, OPENROUTER_KEY_VALUE)
    monkeypatch.setenv(NOTION_TOKEN_ENV_NAME, NOTION_TOKEN_VALUE)
    backend = _agno_backend(monkeypatch)
    _patch_publish(monkeypatch)

    # A stale digest from an earlier run.
    rec_dir = tmp_path / "rec"
    rec_dir.mkdir()
    stale = rec_dir / "meeting.telegram.md"
    stale.write_text("OLD STALE DIGEST\n", encoding="utf-8")

    # This run's model returns an empty digest.
    FakeAgent.digest_text = "   "
    tp = write_transcript(rec_dir)
    cfg = make_config(backend="agno")

    result = backend.summarize(tp, None, rec_dir, cfg)

    # The stale digest is removed rather than left to be re-disseminated.
    assert not stale.exists()
    assert result.summary_path.read_text(encoding="utf-8").strip() != ""


def test_agno_timeout_fails_stage(tmp_path, monkeypatch):
    monkeypatch.setenv(OPENROUTER_KEY_ENV, OPENROUTER_KEY_VALUE)
    monkeypatch.setenv(NOTION_TOKEN_ENV_NAME, NOTION_TOKEN_VALUE)
    backend = _agno_backend(monkeypatch)
    _patch_publish(monkeypatch)

    import transcriber.backends.summarize_agno as sa

    monkeypatch.setattr(sa, "_summarize_timeout", lambda config: 0.05)

    cfg = make_config(backend="agno")
    rec_dir = tmp_path / "rec"
    rec_dir.mkdir()
    tp = write_transcript(rec_dir)

    async def slow_summary(self, prompt):
        await asyncio.sleep(5)  # exceeds the 0.05s bound -> TimeoutError

    # Patch the first (summary) call to hang.
    monkeypatch.setattr(FakeAgent, "arun", slow_summary)
    with pytest.raises(SummarizeError, match="timed out"):
        backend.summarize(tp, None, rec_dir, cfg)


def test_agno_uses_summarize_timeout_when_set(monkeypatch):
    from transcriber.backends.summarize_agno import _summarize_timeout

    cfg = make_config(backend="agno", summarize_timeout=123)
    assert _summarize_timeout(cfg) == 123.0


def test_agno_falls_back_to_agy_timeout(monkeypatch):
    from transcriber.backends.summarize_agno import _summarize_timeout

    cfg = make_config(backend="agno", summarize_timeout=None)
    # timeouts.agy is 42 in make_config.
    assert _summarize_timeout(cfg) == 42.0


def test_agno_missing_extra_raises_actionable_error(tmp_path, monkeypatch):
    monkeypatch.setenv(OPENROUTER_KEY_ENV, OPENROUTER_KEY_VALUE)
    monkeypatch.setenv(NOTION_TOKEN_ENV_NAME, NOTION_TOKEN_VALUE)
    # Install fakes to import the backend class, then force the agno import to
    # fail at run time.
    _install_fake_agno(monkeypatch)
    from transcriber.backends.summarize_agno import AgnoSummarizeBackend
    from transcriber.backends.notion_mcp import AgnoImportError

    backend = AgnoSummarizeBackend()
    _force_missing_agno(monkeypatch)

    cfg = make_config(backend="agno")
    rec_dir = tmp_path / "rec"
    rec_dir.mkdir()
    tp = write_transcript(rec_dir)

    with pytest.raises(AgnoImportError) as excinfo:
        backend.summarize(tp, None, rec_dir, cfg)
    msg = str(excinfo.value)
    assert "agno" in msg and "extra" in msg


# --------------------------------------------------------------------------- #
# Log hygiene (E8): key / token / image bytes never logged
# --------------------------------------------------------------------------- #
def test_agno_log_hygiene_no_secrets(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv(OPENROUTER_KEY_ENV, OPENROUTER_KEY_VALUE)
    monkeypatch.setenv(NOTION_TOKEN_ENV_NAME, NOTION_TOKEN_VALUE)
    backend = _agno_backend(monkeypatch)
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
