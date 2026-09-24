"""Tests for ``transcriber.backends.notion_mcp`` (agno/MCPTools fully mocked).

``agno`` is an optional extra and is not installed in the test environment, so
these tests inject a fake ``agno.tools.mcp`` module (or force an ImportError)
into ``sys.modules`` before the lazy import runs. No real MCP subprocess is
spawned and no network calls are made.
"""

from __future__ import annotations

import os
import sys
import types

import pytest

from transcriber.backends import notion_mcp
from transcriber.backends.notion_mcp import (
    NOTION_MCP_COMMAND,
    NOTION_MCP_PACKAGE,
    NOTION_MCP_TOKEN_ENV,
    AgnoImportError,
    notion_mcp_command,
    notion_mcp_env,
    notion_mcp_tools,
)
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

NOTION_TOKEN_ENV_NAME = "MY_NOTION_TOKEN"
NOTION_TOKEN_VALUE = "ntn_secret_value_should_not_leak"


def make_config(*, token_env: str | None = NOTION_TOKEN_ENV_NAME) -> Config:
    return Config(
        recordings_dir="./recordings",
        stages=Stages(slides=SlidesStage(enabled=False), s3_sync=False),
        transcribe=Transcribe(model_id="scribe_v1"),
        summary=Summary(language="en", sections=[], backend="agno"),
        agent=Agent(cli="agy", extra_args=[], output_file="{basename}.md"),
        notion=Notion(
            server="n",
            parent_page_id="parent-123",
            insert="subpage",
            token_env=token_env,
        ),
        telegram=Telegram(
            bot_token_env="TG", default_chat_id="c", routing={}
        ),
        timeouts=Timeouts(),
        s3=None,
    )


class FakeMCPTools:
    """Stand-in for ``agno.tools.mcp.MCPTools`` recording construction + lifecycle."""

    instances: list["FakeMCPTools"] = []

    def __init__(self, *, command: str, env: dict[str, str]) -> None:
        self.command = command
        self.env = env
        self.closed = False
        self.connected = False
        FakeMCPTools.instances.append(self)

    async def __aenter__(self) -> "FakeMCPTools":
        self.connected = True
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        self.closed = True
        return False  # do not suppress exceptions

    def close(self) -> None:
        self.closed = True


@pytest.fixture(autouse=True)
def _reset_fake_instances():
    FakeMCPTools.instances = []
    yield
    FakeMCPTools.instances = []


def _install_fake_agno(monkeypatch, mcptools_cls=FakeMCPTools) -> None:
    """Inject a fake ``agno.tools.mcp`` module exposing ``MCPTools``."""
    agno_mod = types.ModuleType("agno")
    tools_mod = types.ModuleType("agno.tools")
    mcp_mod = types.ModuleType("agno.tools.mcp")
    mcp_mod.MCPTools = mcptools_cls  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "agno", agno_mod)
    monkeypatch.setitem(sys.modules, "agno.tools", tools_mod)
    monkeypatch.setitem(sys.modules, "agno.tools.mcp", mcp_mod)


def _force_missing_agno(monkeypatch) -> None:
    """Make ``import agno.tools.mcp`` raise ImportError."""
    for name in ("agno", "agno.tools", "agno.tools.mcp"):
        monkeypatch.setitem(sys.modules, name, None)


# --------------------------------------------------------------------------- #
# Constants / command form
# --------------------------------------------------------------------------- #
def test_official_notion_package_and_token_env():
    assert NOTION_MCP_PACKAGE == "@notionhq/notion-mcp-server"
    assert NOTION_MCP_TOKEN_ENV == "NOTION_TOKEN"


def test_command_is_windows_launchable():
    cmd = notion_mcp_command()
    assert cmd == NOTION_MCP_COMMAND
    assert NOTION_MCP_PACKAGE in cmd
    assert "-y" in cmd
    # On Windows, a bare ``npx`` fails to spawn; require the ``.cmd`` shim.
    if sys.platform == "win32":
        assert "npx.cmd" in cmd
    else:
        assert cmd.startswith("npx ")


# --------------------------------------------------------------------------- #
# Env-merge + token-by-name
# --------------------------------------------------------------------------- #
def test_env_is_merged_not_replaced_and_token_by_name(monkeypatch):
    # A sentinel key that lives in os.environ must survive the merge.
    monkeypatch.setenv("PATH_SENTINEL_XYZ", "keep-me")
    monkeypatch.setenv(NOTION_TOKEN_ENV_NAME, NOTION_TOKEN_VALUE)

    env = notion_mcp_env(make_config())

    # Sentinel from os.environ survives (merge, not a bare replace).
    assert env["PATH_SENTINEL_XYZ"] == "keep-me"
    # The resolved token is injected under the official env-var name.
    assert env[NOTION_MCP_TOKEN_ENV] == NOTION_TOKEN_VALUE
    # It is a distinct copy, not os.environ itself.
    assert env is not os.environ


def test_token_resolved_by_name_from_configured_env(monkeypatch):
    # Token is read from the NAME in notion.token_env, not inlined in config.
    monkeypatch.setenv(NOTION_TOKEN_ENV_NAME, NOTION_TOKEN_VALUE)
    env = notion_mcp_env(make_config(token_env=NOTION_TOKEN_ENV_NAME))
    assert env[NOTION_MCP_TOKEN_ENV] == NOTION_TOKEN_VALUE


def test_missing_token_env_name_raises(monkeypatch):
    with pytest.raises(ValueError, match="notion.token_env"):
        notion_mcp_env(make_config(token_env=None))


def test_token_never_logged(monkeypatch, capsys):
    monkeypatch.setenv(NOTION_TOKEN_ENV_NAME, NOTION_TOKEN_VALUE)
    notion_mcp_env(make_config())
    captured = capsys.readouterr()
    assert NOTION_TOKEN_VALUE not in captured.out
    assert NOTION_TOKEN_VALUE not in captured.err


# --------------------------------------------------------------------------- #
# Factory launches MCP with env-merged token
# --------------------------------------------------------------------------- #
def test_factory_launches_official_notion_mcp_with_env_merged_token(monkeypatch):
    monkeypatch.setenv("PATH_SENTINEL_XYZ", "keep-me")
    monkeypatch.setenv(NOTION_TOKEN_ENV_NAME, NOTION_TOKEN_VALUE)
    _install_fake_agno(monkeypatch)

    mcp = notion_mcp_tools(make_config())

    assert isinstance(mcp, FakeMCPTools)
    # Launches the official Notion MCP server via the windows-launchable command.
    assert mcp.command == NOTION_MCP_COMMAND
    assert NOTION_MCP_PACKAGE in mcp.command
    # Env is merged: sentinel survives AND token present under official name.
    assert mcp.env["PATH_SENTINEL_XYZ"] == "keep-me"
    assert mcp.env[NOTION_MCP_TOKEN_ENV] == NOTION_TOKEN_VALUE


# --------------------------------------------------------------------------- #
# close() runs on the exception path
# --------------------------------------------------------------------------- #
def test_mcptools_closed_on_exception_path(monkeypatch):
    """The MCPTools context manager must exit (close) even when the agent raises."""
    import asyncio

    monkeypatch.setenv(NOTION_TOKEN_ENV_NAME, NOTION_TOKEN_VALUE)
    _install_fake_agno(monkeypatch)

    mcp = notion_mcp_tools(make_config())

    class Boom(RuntimeError):
        pass

    async def run_and_fail() -> None:
        async with mcp:
            raise Boom("agent run failed")

    with pytest.raises(Boom):
        asyncio.run(run_and_fail())

    # __aexit__ ran despite the exception -> subprocess not orphaned.
    assert mcp.closed is True


# --------------------------------------------------------------------------- #
# Missing agno import -> clear actionable error
# --------------------------------------------------------------------------- #
def test_missing_agno_import_raises_actionable_error(monkeypatch):
    monkeypatch.setenv(NOTION_TOKEN_ENV_NAME, NOTION_TOKEN_VALUE)
    _force_missing_agno(monkeypatch)

    with pytest.raises(AgnoImportError) as excinfo:
        notion_mcp_tools(make_config())

    msg = str(excinfo.value)
    assert "agno" in msg
    assert "extra" in msg
    # Actionable install hint.
    assert "uv sync --extra agno" in msg or "transcriber[agno]" in msg


# --------------------------------------------------------------------------- #
# No reference to agy's MCP config anywhere in the agno path
# --------------------------------------------------------------------------- #
def test_no_agy_mcp_config_reference_in_module():
    import inspect

    src = inspect.getsource(notion_mcp)
    lowered = src.lower()
    # The agno path is self-contained; it must not read/reuse agy's MCP wiring.
    for forbidden in ("agy_mcp", "agy.mcp", "agy_bridge", "agy-mcp", "agy-mcp-server"):
        assert forbidden not in lowered
    # No import of any agy module in the agno launch path.
    assert "import agy" not in lowered
    assert "from agy" not in lowered
