"""Launch the **official** Notion MCP server for the Agno summarize backend.

This module is engine-owned and self-contained: it configures the Notion MCP
integration purely from ``notion.token_env`` (+ the existing
``notion.parent_page_id`` / ``notion.insert``). It does **not** read or reuse
``agy``'s MCP configuration — the ``agno`` summarize path is independent.

Design notes
------------
* **Official server.** We launch ``@notionhq/notion-mcp-server`` (the official
  Notion MCP server) over its default STDIO transport. It reads the Notion
  integration token from the ``NOTION_TOKEN`` environment variable.
* **Windows-launchable command.** On Windows a bare ``npx`` frequently fails to
  spawn from a subprocess (there is no ``npx`` executable on ``PATH`` — only
  ``npx.cmd``). We therefore build the command with ``npx.cmd`` on Windows and
  ``npx`` elsewhere. This is the most likely first-run blocker.
* **Env is MERGED, never replaced.** The MCP subprocess inherits the parent
  environment (so ``PATH`` / ``APPDATA`` / ``SystemRoot`` survive on Windows)
  and only *adds* the resolved Notion token under ``NOTION_TOKEN``. We never
  hand the child a bare ``{NOTION_TOKEN: ...}`` dict.
* **Secrets by name only.** The token is resolved at *use* time via
  :func:`~transcriber.config.resolve_env` from the env-var *name* in
  ``config.notion.token_env``. The token value is never logged.
* **Lifecycle.** :func:`notion_mcp_tools` only *constructs* the ``MCPTools``
  instance. The caller (Task 4's Agno run wrapper) is responsible for opening
  and closing it — ``MCPTools`` must be closed on every path
  (success/error/timeout) so no MCP subprocess is orphaned.

``agno`` is imported **lazily** inside this module so the engine only requires
the ``agno`` extra when ``summary.backend == "agno"``. A missing import raises a
clear, actionable "install the agno extra" error.
"""

from __future__ import annotations

import os
import sys
from typing import TYPE_CHECKING, Any

from transcriber.config import Config, resolve_env

if TYPE_CHECKING:  # pragma: no cover - typing only
    from agno.tools.mcp import MCPTools

__all__ = [
    "NOTION_MCP_PACKAGE",
    "NOTION_MCP_TOKEN_ENV",
    "NOTION_MCP_COMMAND",
    "AgnoImportError",
    "notion_mcp_command",
    "notion_mcp_env",
    "notion_mcp_tools",
]

# --------------------------------------------------------------------------- #
# Engine-owned launch constants for the OFFICIAL Notion MCP server.
# --------------------------------------------------------------------------- #

#: npm package for the official Notion MCP server (makenotion/notion-mcp-server).
NOTION_MCP_PACKAGE = "@notionhq/notion-mcp-server"

#: Environment-variable name the official Notion MCP server reads its token from.
#: (The recommended ``NOTION_TOKEN`` form, not the advanced ``OPENAPI_MCP_HEADERS``.)
NOTION_MCP_TOKEN_ENV = "NOTION_TOKEN"


def _npx_executable() -> str:
    """Return a Windows-launchable ``npx`` command name.

    A bare ``npx`` typically fails to spawn from a Python subprocess on Windows
    because only the ``npx.cmd`` shim is on ``PATH``. Use ``npx.cmd`` there.
    """
    return "npx.cmd" if sys.platform == "win32" else "npx"


#: Default STDIO command string that launches the official Notion MCP server.
#: ``-y`` auto-confirms the one-time ``npx`` package install prompt.
NOTION_MCP_COMMAND = f"{_npx_executable()} -y {NOTION_MCP_PACKAGE}"


class AgnoImportError(ImportError):
    """Raised when the optional ``agno`` extra is not installed."""


def notion_mcp_command() -> str:
    """Return the Windows-launchable STDIO command for the Notion MCP server."""
    return NOTION_MCP_COMMAND


def notion_mcp_env(config: Config) -> dict[str, str]:
    """Build the MCP subprocess environment.

    The parent environment (``os.environ``) is **merged** with the resolved
    Notion token under :data:`NOTION_MCP_TOKEN_ENV`. This preserves ``PATH`` /
    ``APPDATA`` / ``SystemRoot`` (required to spawn ``npx`` on Windows) while
    injecting the token. It is never a bare replace.

    The token is resolved by env-var *name* from ``config.notion.token_env`` and
    is never logged.

    Raises:
        ValueError: if ``notion.token_env`` is unset (should be caught earlier
            by config validation for ``summary.backend == 'agno'``).
    """
    token_env = config.notion.token_env
    if not token_env:
        raise ValueError(
            "notion.token_env is required to launch the Notion MCP server "
            "(summary.backend == 'agno')"
        )
    token = resolve_env(token_env)
    env = dict(os.environ)  # inherit parent env (PATH/APPDATA/... survive)
    env[NOTION_MCP_TOKEN_ENV] = token
    return env


def _import_mcptools() -> "type[MCPTools]":
    """Import ``agno``'s ``MCPTools`` lazily with an actionable error."""
    try:
        from agno.tools.mcp import MCPTools  # noqa: WPS433 (intentional lazy import)
    except ImportError as exc:  # pragma: no cover - exercised via monkeypatch in tests
        raise AgnoImportError(
            "The 'agno' extra is required for summary.backend == 'agno' "
            "(Agno + Notion MCP). Install it with: "
            "`uv sync --extra agno` (or `pip install 'transcriber[agno]'`)."
        ) from exc
    return MCPTools


def notion_mcp_tools(config: Config) -> "MCPTools":
    """Construct an ``MCPTools`` that launches the official Notion MCP server.

    The returned instance is **not** connected — the caller opens it (via the
    async context manager or ``connect()``) and is responsible for closing it on
    every path so no MCP subprocess is orphaned.

    Args:
        config: The loaded engine config. Uses ``notion.token_env`` only.

    Returns:
        An unconnected ``agno.tools.mcp.MCPTools`` bound to the official Notion
        MCP server, with the Notion token merged into the inherited environment.

    Raises:
        AgnoImportError: if the optional ``agno`` extra is not installed.
        ValueError: if ``notion.token_env`` is unset.
    """
    mcptools_cls: Any = _import_mcptools()
    command = notion_mcp_command()
    env = notion_mcp_env(config)
    return mcptools_cls(command=command, env=env)
