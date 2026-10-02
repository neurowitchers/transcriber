"""Telegram dissemination via the Bot HTTP API.

Sends summary text to Telegram chats using the Bot API ``sendMessage``
endpoint (``https://api.telegram.org/bot<token>/sendMessage``) over ``httpx``.

Responsibilities of this module:

* Resolve the bot token from the environment variable **named** in
  ``config.telegram.bot_token_env`` (never a literal secret).
* Route a message to a chat id based on its topic:
  matched topics use ``config.telegram.routing[topic]``; unmatched or
  unidentified topics fall back to ``config.telegram.default_chat_id``.
* Chunk messages that exceed Telegram's per-message character limit into
  multiple ``sendMessage`` calls.

Message language is decided upstream (per ``config.summary.language``); the
caller supplies the final text and this module only routes and sends it.
"""

from __future__ import annotations

import re
from typing import Optional

import httpx

from transcriber.config import Config, Telegram, resolve_env

__all__ = [
    "TELEGRAM_MAX_MESSAGE_CHARS",
    "TelegramError",
    "TelegramPublisher",
    "resolve_chat_id",
    "chunk_message",
    "strip_markdown",
]

# Telegram's hard limit for a single text message.
TELEGRAM_MAX_MESSAGE_CHARS = 4096

_API_BASE = "https://api.telegram.org"


# --------------------------------------------------------------------------- #
# Markdown -> clean plain text
# --------------------------------------------------------------------------- #
# The digest is LLM-generated Markdown (``**bold**``, ``#`` headings, ``-``
# bullets, ``[text](url)`` links). Telegram ``sendMessage`` is called WITHOUT a
# ``parse_mode``, so Markdown syntax would otherwise render literally (the user
# sees the raw ``**``/``#``/``-`` characters). Rather than enable a Telegram
# parse mode (MarkdownV2 needs pervasive escaping and rejects unescaped
# specials; Telegram has no heading/list formatting anyway), we strip the
# Markdown to clean, readable plain text before sending.

_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\(([^)]+)\)")
_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+")
_BULLET_RE = re.compile(r"^(\s*)[-*+]\s+")
_BLOCKQUOTE_RE = re.compile(r"^\s{0,3}>\s?")
_BOLD_RE = re.compile(r"(\*\*|__)(.+?)\1", re.DOTALL)
_ITALIC_RE = re.compile(r"(?<!\*)\*(?!\*)([^*\n]+?)\*(?!\*)|(?<!_)_(?!_)([^_\n]+?)_(?!_)")
_INLINE_CODE_RE = re.compile(r"`([^`]+)`")

# Bullet glyph used for Markdown list items (Telegram has no list formatting).
# Defined via an explicit escape so the source stays ASCII and the character is
# unambiguous regardless of file encoding.
_BULLET_GLYPH = "\u2022"  # •


def strip_markdown(text: str) -> str:
    """Return ``text`` with Markdown syntax reduced to clean plain text.

    Telegram messages are sent with no ``parse_mode``, so Markdown markers would
    render literally. This converts the common Markdown the digest uses into
    readable plain text:

    * ``**bold**`` / ``__bold__`` / ``*italic*`` / ``_italic_`` -> inner text.
    * `````code````` -> inner text (backticks removed).
    * leading ``#`` heading markers -> removed (Telegram has no headings).
    * ``-`` / ``*`` / ``+`` list bullets -> a ``•`` glyph (indentation kept).
    * ``> quote`` -> the quoted text (marker removed).
    * ``[text](url)`` -> ``text (url)`` so the link target is not lost;
      ``![alt](url)`` images -> ``alt (url)`` (or just the url when no alt).
    * fenced-code fences (```` ``` ````) -> removed (content kept verbatim).

    Line structure is preserved so the message stays readable.
    """
    # Links / images first (before emphasis, so '*' inside URLs is untouched).
    text = _IMAGE_RE.sub(
        lambda m: f"{m.group(1)} ({m.group(2)})" if m.group(1) else m.group(2),
        text,
    )
    text = _LINK_RE.sub(lambda m: f"{m.group(1)} ({m.group(2)})", text)

    out_lines: list[str] = []
    for line in text.splitlines():
        # Drop fenced-code fences entirely (keep any content lines verbatim).
        if line.lstrip().startswith("```"):
            continue
        line = _HEADING_RE.sub("", line)
        line = _BLOCKQUOTE_RE.sub("", line)
        bullet = _BULLET_RE.match(line)
        if bullet:
            line = f"{bullet.group(1)}{_BULLET_GLYPH} " + line[bullet.end():]
        # Inline emphasis / code (order: bold before italic so ** is consumed).
        line = _BOLD_RE.sub(lambda m: m.group(2), line)
        line = _ITALIC_RE.sub(lambda m: m.group(1) or m.group(2), line)
        line = _INLINE_CODE_RE.sub(lambda m: m.group(1), line)
        out_lines.append(line)
    return "\n".join(out_lines)


class TelegramError(RuntimeError):
    """Raised when a Telegram ``sendMessage`` call fails."""


def resolve_chat_id(telegram: Telegram, topic: Optional[str]) -> str:
    """Resolve the destination chat id for a given topic.

    Matched topics resolve to their configured chat id in
    ``telegram.routing``. An unmatched topic, or a ``None``/empty
    (unidentified) topic, falls back to ``telegram.default_chat_id``.
    """
    if topic:
        chat_id = telegram.routing.get(topic)
        if chat_id:
            return chat_id
    return telegram.default_chat_id


def chunk_message(text: str, limit: int = TELEGRAM_MAX_MESSAGE_CHARS) -> list[str]:
    """Split ``text`` into chunks no longer than ``limit`` characters.

    An empty string yields a single empty chunk so that at least one
    ``sendMessage`` call is made. Splitting is purely by character count;
    the caller is responsible for any content-aware formatting.
    """
    if limit <= 0:
        raise ValueError("limit must be a positive integer")
    if text == "":
        return [""]
    return [text[i : i + limit] for i in range(0, len(text), limit)]


class TelegramPublisher:
    """Sends messages to Telegram chats via the Bot HTTP API.

    Args:
        config: The loaded :class:`~transcriber.config.Config`.
        client: Optional pre-configured ``httpx.Client``. If omitted, a
            client is created per :meth:`send` call. Tests inject a mocked
            client here.
    """

    def __init__(
        self, config: Config, client: Optional[httpx.Client] = None
    ) -> None:
        self._config = config
        self._telegram = config.telegram
        self._client = client

    def _token(self) -> str:
        """Resolve the bot token from the configured env var name.

        Raises:
            TelegramError: if the referenced environment variable is not set,
                with an actionable message naming the variable.
        """
        from transcriber.config import MissingEnvVarError

        try:
            return resolve_env(self._telegram.bot_token_env)
        except MissingEnvVarError as exc:
            raise TelegramError(
                "Telegram bot token is unavailable: environment variable "
                f"'{self._telegram.bot_token_env}' "
                "(from telegram.bot_token_env) is not set. "
                "Export it before publishing to Telegram."
            ) from exc

    def _send_message_url(self, token: str) -> str:
        return f"{_API_BASE}/bot{token}/sendMessage"

    def send(self, text: str, topic: Optional[str] = None) -> list[httpx.Response]:
        """Route and send ``text`` to the chat for ``topic``.

        The message is chunked to respect Telegram's length limit; each chunk
        is sent as a separate ``sendMessage`` call.

        Args:
            text: The message body (language handled upstream).
            topic: Topic used for routing; ``None``/unmatched -> default chat.

        Returns:
            The list of ``httpx.Response`` objects, one per chunk sent.

        Raises:
            TelegramError: if the token env var is missing or a call fails.
        """
        token = self._token()
        chat_id = resolve_chat_id(self._telegram, topic)
        url = self._send_message_url(token)
        # Telegram is called with no parse_mode, so strip Markdown to clean
        # plain text first — otherwise the raw ``**``/``#``/``-`` markers render
        # literally in the chat.
        chunks = chunk_message(strip_markdown(text))

        if self._client is not None:
            return self._send_all(self._client, url, chat_id, chunks)

        with httpx.Client() as client:
            return self._send_all(client, url, chat_id, chunks)

    def _send_all(
        self,
        client: httpx.Client,
        url: str,
        chat_id: str,
        chunks: list[str],
    ) -> list[httpx.Response]:
        responses: list[httpx.Response] = []
        for chunk in chunks:
            payload = {"chat_id": chat_id, "text": chunk}
            try:
                response = client.post(url, json=payload)
                response.raise_for_status()
            except httpx.HTTPError as exc:
                raise TelegramError(
                    f"Telegram sendMessage failed for chat '{chat_id}': {exc}"
                ) from exc
            responses.append(response)
        return responses
