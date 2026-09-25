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

    def __init__(self, *, command: str, env: dict, include_tools=None) -> None:
        self.command = command
        self.env = env
        self.include_tools = include_tools
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


class _FakeToolCall:
    def __init__(self, name: str, error=False) -> None:
        self.tool_name = name
        self.tool_call_error = error


class FakeRunOutput:
    def __init__(self, content, tools=None) -> None:
        self.content = content
        self.tools = tools or []

    def get_content_as_string(self) -> str:
        return self.content if isinstance(self.content, str) else ""


class FakeAgent:
    instances: list["FakeAgent"] = []

    # summary_text / digest_text: plain-text returned by the two no-tool calls
    # (phase 1 summary, phase 1b digest). publish_behavior: optional callable
    # for the phase-2 (MCP tools) publish call.
    summary_text = "# Summary\n\nBody."
    digest_text = "Title\n- Decision"
    publish_behavior = None
    # Tool calls the publish agent "made"; default is one successful create so
    # _verify_published passes. Tests can set [] (no-op) or an errored call.
    publish_tools = None
    _plain_calls = 0

    def __init__(self, *, model, tools=None, output_schema=None) -> None:
        self.model = model
        self.tools = tools or []
        self.output_schema = output_schema
        self.arun_called_with = None
        self.is_publish = bool(self.tools)
        FakeAgent.instances.append(self)

    async def arun(self, prompt: str):
        self.arun_called_with = prompt
        if self.is_publish:
            if FakeAgent.publish_behavior is not None:
                await FakeAgent.publish_behavior(prompt)
            tools = FakeAgent.publish_tools
            if tools is None:
                tools = [_FakeToolCall("API-post-page")]
            return FakeRunOutput("published ok", tools=tools)
        # No-tool calls: first is the summary, second is the digest.
        FakeAgent._plain_calls += 1
        text = (
            FakeAgent.summary_text
            if FakeAgent._plain_calls == 1
            else FakeAgent.digest_text
        )
        return FakeRunOutput(text)


@pytest.fixture(autouse=True)
def _reset_fakes():
    FakeMCPTools.instances = []
    FakeOpenRouterModel.instances = []
    FakeAgent.instances = []
    FakeAgent.summary_text = "# Summary\n\nBody."
    FakeAgent.digest_text = "Title\n- Decision"
    FakeAgent.publish_behavior = None
    FakeAgent.publish_tools = None
    FakeAgent._plain_calls = 0
    yield
    FakeAgent.publish_behavior = None
    FakeAgent.publish_tools = None
    FakeAgent._plain_calls = 0


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

    result = backend.summarize(tp, None, rec_dir, cfg)

    # Three model instances built (summary + digest + publish), all with the
    # configured summary_model + resolved key/base_url.
    assert len(FakeOpenRouterModel.instances) == 3
    for model in FakeOpenRouterModel.instances:
        assert model.id == "anthropic/claude-3.5-sonnet"
        assert model.api_key == OPENROUTER_KEY_VALUE
        assert model.base_url == "https://openrouter.ai/api/v1"

    # Notion MCP attached (publish phase) with the token resolved by env name.
    assert len(FakeMCPTools.instances) == 1
    mcp = FakeMCPTools.instances[0]
    assert mcp.command == NOTION_MCP_COMMAND
    assert mcp.env[NOTION_MCP_TOKEN_ENV] == NOTION_TOKEN_VALUE

    # Three agents: two no-tool (summary, digest) + one publish (MCP tool).
    assert len(FakeAgent.instances) == 3
    plain = [a for a in FakeAgent.instances if not a.is_publish]
    publish = [a for a in FakeAgent.instances if a.is_publish]
    assert len(plain) == 2 and len(publish) == 1
    assert mcp in publish[0].tools

    # Engine wrote the files from the plain-text summary + digest.
    assert isinstance(result, SummaryResult)
    assert "Body." in result.summary_path.read_text(encoding="utf-8")
    assert result.telegram_path.read_text(encoding="utf-8").strip() == (
        "Title\n- Decision"
    )
    assert mcp.closed is True  # closed on the publish path


def test_agno_prompts_carry_slides_and_notion_id(tmp_path, monkeypatch):
    monkeypatch.setenv(OPENROUTER_KEY_ENV, OPENROUTER_KEY_VALUE)
    monkeypatch.setenv(NOTION_TOKEN_ENV_NAME, NOTION_TOKEN_VALUE)
    backend = _agno_backend(monkeypatch)

    cfg = make_config(backend="agno")
    rec_dir = tmp_path / "rec"
    rec_dir.mkdir()
    tp = write_transcript(rec_dir)
    slides_md = "### Slide 1\n**Timestamp:** 00:00 - 00:10\n\nIntro.\n"

    backend.summarize(tp, slides_md, rec_dir, cfg)

    plain = [a for a in FakeAgent.instances if not a.is_publish]
    publish = [a for a in FakeAgent.instances if a.is_publish][0]
    # Phase 1 (first no-tool call): transcript + slide markdown inlined.
    assert slides_md in plain[0].arun_called_with
    # Publish phase: the Notion parent-page id + the produced summary text.
    assert "PARENT-PAGE-ID-123" in publish.arun_called_with
    assert "Body." in publish.arun_called_with
    # Publish prompt names the real MCP create tool (no hallucinated names).
    assert "API-post-page" in publish.arun_called_with


def test_agno_empty_summary_fails_stage(tmp_path, monkeypatch):
    monkeypatch.setenv(OPENROUTER_KEY_ENV, OPENROUTER_KEY_VALUE)
    monkeypatch.setenv(NOTION_TOKEN_ENV_NAME, NOTION_TOKEN_VALUE)
    backend = _agno_backend(monkeypatch)

    cfg = make_config(backend="agno")
    rec_dir = tmp_path / "rec"
    rec_dir.mkdir()
    tp = write_transcript(rec_dir)

    # Phase-1 summary comes back whitespace-only -> fail before publishing.
    FakeAgent.summary_text = "   "
    with pytest.raises(SummarizeError, match="no usable summary"):
        backend.summarize(tp, None, rec_dir, cfg)
    # Publish MCP never constructed (we bailed before phase 2).
    assert len(FakeMCPTools.instances) == 0


def test_agno_mcp_closed_on_exception_path(tmp_path, monkeypatch):
    monkeypatch.setenv(OPENROUTER_KEY_ENV, OPENROUTER_KEY_VALUE)
    monkeypatch.setenv(NOTION_TOKEN_ENV_NAME, NOTION_TOKEN_VALUE)
    backend = _agno_backend(monkeypatch)

    cfg = make_config(backend="agno")
    rec_dir = tmp_path / "rec"
    rec_dir.mkdir()
    tp = write_transcript(rec_dir)

    async def publish_behavior(prompt):
        raise RuntimeError("publish boom mid-run")

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

    import transcriber.backends.summarize_agno as sa

    monkeypatch.setattr(sa, "_summarize_timeout", lambda config: 0.05)

    cfg = make_config(backend="agno")
    rec_dir = tmp_path / "rec"
    rec_dir.mkdir()
    tp = write_transcript(rec_dir)

    async def publish_behavior(prompt):
        await asyncio.sleep(5)  # publish (MCP open) exceeds the 0.05s bound

    FakeAgent.publish_behavior = staticmethod(publish_behavior)
    with pytest.raises(SummarizeError, match="timed out"):
        backend.summarize(tp, None, rec_dir, cfg)

    # MCPTools closed on the timeout path too (cancellation propagates into
    # the async-with body -> __aexit__ runs).
    assert len(FakeMCPTools.instances) == 1
    assert FakeMCPTools.instances[0].closed is True


def test_agno_no_tool_call_fails_stage(tmp_path, monkeypatch):
    monkeypatch.setenv(OPENROUTER_KEY_ENV, OPENROUTER_KEY_VALUE)
    monkeypatch.setenv(NOTION_TOKEN_ENV_NAME, NOTION_TOKEN_VALUE)
    backend = _agno_backend(monkeypatch)

    cfg = make_config(backend="agno")
    rec_dir = tmp_path / "rec"
    rec_dir.mkdir()
    tp = write_transcript(rec_dir)

    # Publish agent replies in prose but calls no tool -> nothing published.
    FakeAgent.publish_tools = []
    with pytest.raises(SummarizeError, match="no successful Notion MCP tool call"):
        backend.summarize(tp, None, rec_dir, cfg)
    # MCP still closed (lifecycle intact) even though we reject the result.
    assert FakeMCPTools.instances[0].closed is True


def test_agno_errored_tool_call_fails_stage(tmp_path, monkeypatch):
    monkeypatch.setenv(OPENROUTER_KEY_ENV, OPENROUTER_KEY_VALUE)
    monkeypatch.setenv(NOTION_TOKEN_ENV_NAME, NOTION_TOKEN_VALUE)
    backend = _agno_backend(monkeypatch)

    cfg = make_config(backend="agno")
    rec_dir = tmp_path / "rec"
    rec_dir.mkdir()
    tp = write_transcript(rec_dir)

    # Only an errored tool call (e.g. hallucinated tool name rejected by MCP).
    FakeAgent.publish_tools = [_FakeToolCall("post_page", error=True)]
    with pytest.raises(SummarizeError, match="no successful Notion MCP tool call"):
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
