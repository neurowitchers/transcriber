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
from transcriber.backends.notion_mcp import (
    NOTION_MCP_COMMAND,
    NOTION_MCP_TOKEN_ENV,
)
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


def _summary_content(summary: str = "# Summary\n\nBody.", digest: str = "Title\n- Decision"):
    """A simple stand-in for the Agno structured-output object (.summary/.digest)."""
    return types.SimpleNamespace(summary=summary, digest=digest)


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
class FakeMCPTools:
    instances: list["FakeMCPTools"] = []

    def __init__(self, *, command: str, env: dict) -> None:
        self.command = command
        self.env = env
        self.closed = False
        FakeMCPTools.instances.append(self)

    async def __aenter__(self) -> "FakeMCPTools":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        self.closed = True
        return False


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
    instances: list["FakeAgent"] = []

    # Phase-1 (summarize) behavior: callable(prompt) -> content object with
    # .summary/.digest (or None). Phase-2 (publish) uses `publish_behavior`.
    behavior = None
    publish_behavior = None

    def __init__(self, *, model, tools=None, output_schema=None) -> None:
        self.model = model
        self.tools = tools or []
        self.output_schema = output_schema
        self.arun_called_with = None
        # Phase 1 has an output_schema and no tools; phase 2 has tools and no
        # output_schema.
        self.is_publish = output_schema is None and bool(self.tools)
        FakeAgent.instances.append(self)

    async def arun(self, prompt: str):
        self.arun_called_with = prompt
        if self.is_publish:
            if FakeAgent.publish_behavior is not None:
                await FakeAgent.publish_behavior(prompt)
            return FakeRunOutput(None)
        # Phase 1: structured summarize.
        content = None
        if FakeAgent.behavior is not None:
            content = await FakeAgent.behavior(prompt)
        return FakeRunOutput(content)


@pytest.fixture(autouse=True)
def _reset_fakes():
    FakeMCPTools.instances = []
    FakeOpenRouterModel.instances = []
    FakeAgent.instances = []
    FakeAgent.behavior = None
    FakeAgent.publish_behavior = None
    yield
    FakeAgent.behavior = None
    FakeAgent.publish_behavior = None


def _install_fake_agno(monkeypatch) -> None:
    agno_mod = types.ModuleType("agno")
    agent_mod = types.ModuleType("agno.agent")
    agent_mod.Agent = FakeAgent  # type: ignore[attr-defined]
    models_mod = types.ModuleType("agno.models")
    openrouter_mod = types.ModuleType("agno.models.openrouter")
    openrouter_mod.OpenRouter = FakeOpenRouterModel  # type: ignore[attr-defined]
    tools_mod = types.ModuleType("agno.tools")
    mcp_mod = types.ModuleType("agno.tools.mcp")
    mcp_mod.MCPTools = FakeMCPTools  # type: ignore[attr-defined]

    # Minimal fake pydantic (base engine has no pydantic; agno brings it).
    pydantic_mod = types.ModuleType("pydantic")

    class _FakeBaseModel:  # noqa: WPS431
        pass

    def _fake_field(default=None, **kwargs):  # noqa: WPS430
        return default

    pydantic_mod.BaseModel = _FakeBaseModel  # type: ignore[attr-defined]
    pydantic_mod.Field = _fake_field  # type: ignore[attr-defined]

    for name, mod in {
        "agno": agno_mod,
        "agno.agent": agent_mod,
        "agno.models": models_mod,
        "agno.models.openrouter": openrouter_mod,
        "agno.tools": tools_mod,
        "agno.tools.mcp": mcp_mod,
        "pydantic": pydantic_mod,
    }.items():
        monkeypatch.setitem(sys.modules, name, mod)


def _force_missing_agno(monkeypatch) -> None:
    for name in (
        "agno",
        "agno.agent",
        "agno.models",
        "agno.models.openrouter",
        "agno.tools",
        "agno.tools.mcp",
    ):
        monkeypatch.setitem(sys.modules, name, None)


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
# AgnoSummarizeBackend — Agno + MCP mocked
# --------------------------------------------------------------------------- #
def _agno_backend(monkeypatch):
    _install_fake_agno(monkeypatch)
    from transcriber.backends.summarize_agno import AgnoSummarizeBackend

    return AgnoSummarizeBackend()


def test_agno_builds_model_and_attaches_notion_mcp(tmp_path, monkeypatch):
    monkeypatch.setenv(OPENROUTER_KEY_ENV, OPENROUTER_KEY_VALUE)
    monkeypatch.setenv(NOTION_TOKEN_ENV_NAME, NOTION_TOKEN_VALUE)
    backend = _agno_backend(monkeypatch)

    cfg = make_config(backend="agno", summary_model="anthropic/claude-3.5-sonnet")
    rec_dir = tmp_path / "rec"
    rec_dir.mkdir()
    tp = write_transcript(rec_dir)

    async def behavior(prompt):
        return _summary_content()

    FakeAgent.behavior = staticmethod(behavior)

    result = backend.summarize(tp, None, rec_dir, cfg)

    # Two model instances built (phase 1 summarize + phase 2 publish), both
    # with summary_model + resolved key/base_url.
    assert len(FakeOpenRouterModel.instances) == 2
    for model in FakeOpenRouterModel.instances:
        assert model.id == "anthropic/claude-3.5-sonnet"
        assert model.api_key == OPENROUTER_KEY_VALUE
        assert model.base_url == "https://openrouter.ai/api/v1"

    # Notion MCP attached (phase 2) with the token resolved by env-var name.
    assert len(FakeMCPTools.instances) == 1
    mcp = FakeMCPTools.instances[0]
    assert mcp.command == NOTION_MCP_COMMAND
    assert mcp.env[NOTION_MCP_TOKEN_ENV] == NOTION_TOKEN_VALUE

    # Two agents: phase 1 (output_schema, no tools) + phase 2 (MCP tool, publish).
    assert len(FakeAgent.instances) == 2
    phase1 = [a for a in FakeAgent.instances if not a.is_publish]
    phase2 = [a for a in FakeAgent.instances if a.is_publish]
    assert len(phase1) == 1 and len(phase2) == 1
    assert phase1[0].output_schema is not None
    assert mcp in phase2[0].tools

    # Engine wrote the files from phase 1's structured output (option 1).
    assert isinstance(result, SummaryResult)
    assert "Body." in result.summary_path.read_text(encoding="utf-8")
    assert result.telegram_path.read_text(encoding="utf-8").strip() == (
        "Title\n- Decision"
    )
    assert mcp.closed is True  # closed on the publish (phase 2) path


def test_agno_runs_shared_prompt_with_slide_markdown(tmp_path, monkeypatch):
    monkeypatch.setenv(OPENROUTER_KEY_ENV, OPENROUTER_KEY_VALUE)
    monkeypatch.setenv(NOTION_TOKEN_ENV_NAME, NOTION_TOKEN_VALUE)
    backend = _agno_backend(monkeypatch)

    cfg = make_config(backend="agno")
    rec_dir = tmp_path / "rec"
    rec_dir.mkdir()
    tp = write_transcript(rec_dir)
    slides_md = "### Slide 1\n**Timestamp:** 00:00 - 00:10\n\nIntro.\n"

    async def behavior(prompt):
        return _summary_content()

    FakeAgent.behavior = staticmethod(behavior)
    backend.summarize(tp, slides_md, rec_dir, cfg)

    phase1 = [a for a in FakeAgent.instances if not a.is_publish][0]
    phase2 = [a for a in FakeAgent.instances if a.is_publish][0]
    # Phase 1 (summarize): transcript + slide markdown inlined, no Notion id.
    assert slides_md in phase1.arun_called_with
    # Phase 2 (publish): the Notion parent-page id + the produced summary.
    assert "PARENT-PAGE-ID-123" in phase2.arun_called_with
    assert "Body." in phase2.arun_called_with


def test_agno_empty_summary_block_fails_stage(tmp_path, monkeypatch):
    monkeypatch.setenv(OPENROUTER_KEY_ENV, OPENROUTER_KEY_VALUE)
    monkeypatch.setenv(NOTION_TOKEN_ENV_NAME, NOTION_TOKEN_VALUE)
    backend = _agno_backend(monkeypatch)

    cfg = make_config(backend="agno")
    rec_dir = tmp_path / "rec"
    rec_dir.mkdir()
    tp = write_transcript(rec_dir)

    async def behavior(prompt):
        # Structured output present but the summary field is whitespace-only.
        return _summary_content(summary="   ", digest="")

    FakeAgent.behavior = staticmethod(behavior)
    with pytest.raises(SummarizeError, match="no usable summary"):
        backend.summarize(tp, None, rec_dir, cfg)


def test_agno_missing_summary_block_fails_stage(tmp_path, monkeypatch):
    monkeypatch.setenv(OPENROUTER_KEY_ENV, OPENROUTER_KEY_VALUE)
    monkeypatch.setenv(NOTION_TOKEN_ENV_NAME, NOTION_TOKEN_VALUE)
    backend = _agno_backend(monkeypatch)

    cfg = make_config(backend="agno")
    rec_dir = tmp_path / "rec"
    rec_dir.mkdir()
    tp = write_transcript(rec_dir)

    async def behavior(prompt):
        # No structured content at all (truncated/off-format run).
        return None

    FakeAgent.behavior = staticmethod(behavior)
    with pytest.raises(SummarizeError, match="no usable summary"):
        backend.summarize(tp, None, rec_dir, cfg)


def test_agno_mcp_closed_on_exception_path(tmp_path, monkeypatch):
    monkeypatch.setenv(OPENROUTER_KEY_ENV, OPENROUTER_KEY_VALUE)
    monkeypatch.setenv(NOTION_TOKEN_ENV_NAME, NOTION_TOKEN_VALUE)
    backend = _agno_backend(monkeypatch)

    cfg = make_config(backend="agno")
    rec_dir = tmp_path / "rec"
    rec_dir.mkdir()
    tp = write_transcript(rec_dir)

    async def behavior(prompt):
        return _summary_content()

    async def publish_behavior(prompt):
        raise RuntimeError("publish boom mid-run")

    FakeAgent.behavior = staticmethod(behavior)
    FakeAgent.publish_behavior = staticmethod(publish_behavior)
    with pytest.raises(SummarizeError):
        backend.summarize(tp, None, rec_dir, cfg)

    # MCPTools closed despite the mid-run exception (subprocess not orphaned).
    assert len(FakeMCPTools.instances) == 1
    assert FakeMCPTools.instances[0].closed is True


def test_agno_timeout_closes_mcp_and_fails_stage(tmp_path, monkeypatch):
    monkeypatch.setenv(OPENROUTER_KEY_ENV, OPENROUTER_KEY_VALUE)
    monkeypatch.setenv(NOTION_TOKEN_ENV_NAME, NOTION_TOKEN_VALUE)
    backend = _agno_backend(monkeypatch)

    # A small positive bound so the coroutine starts (MCP constructed + entered)
    # before the agent's long sleep trips the timeout.
    import transcriber.backends.summarize_agno as sa

    monkeypatch.setattr(sa, "_summarize_timeout", lambda config: 0.05)

    cfg = make_config(backend="agno")
    rec_dir = tmp_path / "rec"
    rec_dir.mkdir()
    tp = write_transcript(rec_dir)

    async def behavior(prompt):
        return _summary_content()  # phase 1 completes quickly

    async def publish_behavior(prompt):
        await asyncio.sleep(5)  # phase 2 (MCP open) exceeds the 0.05s bound

    FakeAgent.behavior = staticmethod(behavior)
    FakeAgent.publish_behavior = staticmethod(publish_behavior)
    with pytest.raises(SummarizeError, match="timed out"):
        backend.summarize(tp, None, rec_dir, cfg)

    # MCPTools closed on the timeout path too (cancellation propagates into
    # the async-with body -> __aexit__ runs).
    assert len(FakeMCPTools.instances) == 1
    assert FakeMCPTools.instances[0].closed is True


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

    cfg = make_config(backend="agno")
    rec_dir = tmp_path / "rec"
    rec_dir.mkdir()
    tp = write_transcript(rec_dir)

    async def behavior(prompt):
        return _summary_content()

    FakeAgent.behavior = staticmethod(behavior)

    with caplog.at_level(logging.DEBUG):
        backend.summarize(tp, None, rec_dir, cfg)

    text = "\n".join(r.getMessage() for r in caplog.records)
    assert OPENROUTER_KEY_VALUE not in text
    assert NOTION_TOKEN_VALUE not in text
    assert IMAGE_BYTES_SENTINEL not in text
