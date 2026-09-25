"""Shared OpenRouter chat-completions helper for the slides vision call.

This module hosts :func:`_openrouter_vision`, the single HTTP seam used by the
slides ``openrouter`` backend (Task 3). The summarize ``agno`` backend does
**not** use this helper — it goes through Agno + MCP (Task 4/6).

Behavior:
    * ``POST {base_url}/chat/completions`` with the caller-provided ``messages``
      and ``model``, honoring ``timeout``.
    * Headers: ``Authorization: Bearer <key>`` (key resolved by env-var name),
      plus OpenRouter's optional ``HTTP-Referer`` and ``X-Title`` attribution
      headers.
    * Retries ``429`` and ``5xx`` with bounded exponential backoff (a small,
      capped number of attempts), then raises
      :class:`~transcriber.backends.errors.SlideDescribeError`.
    * On a non-2xx status (after retries), malformed JSON, or a missing
      ``choices[0].message.content`` → raises ``SlideDescribeError``.

**Log hygiene (R16/R17):** neither the exceptions raised here nor the log lines
emitted include the API key, the request headers, or any base64 image bytes.
Errors carry only the HTTP status and a short, truncated body snippet.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Sequence

import httpx

from transcriber.config import Config, resolve_env
from transcriber.backends.errors import SlideDescribeError

logger = logging.getLogger(__name__)

# OpenRouter attribution headers (public, non-secret).
_HTTP_REFERER = "https://github.com/neurowitchers/transcriber"
_X_TITLE = "transcriber"

# Bounded exponential backoff for transient (429 / 5xx) responses.
MAX_ATTEMPTS = 4  # total tries, including the first
_BACKOFF_BASE_SECONDS = 0.5
_BACKOFF_CAP_SECONDS = 8.0

# Statuses treated as transient and retried.
_RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504})

# Max characters of a response body echoed into an error message. Bounds the
# blast radius of any accidental sensitive content and keeps logs readable.
_SNIPPET_LEN = 200


def _snippet(text: str) -> str:
    """Return a short, single-line body snippet safe to log/raise."""
    collapsed = " ".join(text.split())
    if len(collapsed) > _SNIPPET_LEN:
        return collapsed[:_SNIPPET_LEN] + "…"
    return collapsed


def _backoff_seconds(attempt: int) -> float:
    """Bounded exponential backoff for the given zero-based attempt index."""
    return min(_BACKOFF_BASE_SECONDS * (2**attempt), _BACKOFF_CAP_SECONDS)


def _openrouter_vision(
    config: Config,
    messages: Sequence[dict[str, Any]],
    model: str,
    *,
    timeout: float,
) -> str:
    """Run a single OpenRouter chat-completions call and return the content.

    Args:
        config: The loaded config; ``config.openrouter`` supplies the base URL
            and the API-key env-var name (resolved here at use time).
        messages: The OpenAI-compatible ``messages`` array (already built by
            the caller, including any vision ``image_url`` parts).
        model: The model id to send (e.g. ``config.openrouter.slides_model``).
        timeout: Per-request timeout in seconds.

    Returns:
        The text at ``choices[0].message.content`` (may be an empty string).

    Raises:
        SlideDescribeError: on missing ``openrouter`` config, transport error,
            non-2xx after retries, malformed JSON, or missing content.
    """
    if config.openrouter is None:
        raise SlideDescribeError(
            "openrouter config section is required for the openrouter slides "
            "backend but is not configured"
        )

    api_key = resolve_env(config.openrouter.api_key_env)
    base_url = config.openrouter.base_url.rstrip("/")
    url = f"{base_url}/chat/completions"

    headers = {
        "Authorization": f"Bearer {api_key}",
        "HTTP-Referer": _HTTP_REFERER,
        "X-Title": _X_TITLE,
        "Content-Type": "application/json",
    }
    payload = {"model": model, "messages": list(messages)}

    last_status: int | None = None
    last_snippet: str = ""

    for attempt in range(MAX_ATTEMPTS):
        try:
            response = httpx.post(
                url, headers=headers, json=payload, timeout=timeout
            )
        except httpx.HTTPError as exc:
            # Transport-level failure (connect/read/etc). Retry within the cap.
            # NB: never log the exception's request (it carries the headers).
            last_status = None
            last_snippet = type(exc).__name__
            logger.warning(
                "openrouter request transport error (attempt %d/%d): %s",
                attempt + 1,
                MAX_ATTEMPTS,
                type(exc).__name__,
            )
            if attempt + 1 < MAX_ATTEMPTS:
                time.sleep(_backoff_seconds(attempt))
                continue
            raise SlideDescribeError(
                f"openrouter request failed after {MAX_ATTEMPTS} attempts: "
                f"transport error {type(exc).__name__}"
            ) from exc

        status = response.status_code
        if status in _RETRYABLE_STATUSES:
            last_status = status
            last_snippet = _snippet(response.text)
            logger.warning(
                "openrouter transient status %d (attempt %d/%d)",
                status,
                attempt + 1,
                MAX_ATTEMPTS,
            )
            if attempt + 1 < MAX_ATTEMPTS:
                time.sleep(_backoff_seconds(attempt))
                continue
            raise SlideDescribeError(
                f"openrouter call failed after {MAX_ATTEMPTS} attempts: "
                f"status {status}: {last_snippet}"
            )

        if not (200 <= status < 300):
            raise SlideDescribeError(
                f"openrouter call failed: status {status}: "
                f"{_snippet(response.text)}"
            )

        # 2xx: parse and extract content.
        try:
            data = response.json()
        except ValueError as exc:
            raise SlideDescribeError(
                f"openrouter returned malformed JSON: status {status}: "
                f"{_snippet(response.text)}"
            ) from exc

        content = _extract_content(data)
        if content is None:
            raise SlideDescribeError(
                "openrouter response missing choices[0].message.content: "
                f"status {status}: {_snippet(response.text)}"
            )
        return content

    # Unreachable: the loop either returns or raises on the final attempt.
    raise SlideDescribeError(  # pragma: no cover
        f"openrouter call failed: status {last_status}: {last_snippet}"
    )


def _extract_content(data: Any) -> str | None:
    """Pull ``choices[0].message.content`` out of a parsed response.

    Returns ``None`` when the shape is missing/invalid so the caller can raise
    a clean :class:`SlideDescribeError`.
    """
    if not isinstance(data, dict):
        return None
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        return None
    first = choices[0]
    if not isinstance(first, dict):
        return None
    message = first.get("message")
    if not isinstance(message, dict):
        return None
    content = message.get("content")
    if not isinstance(content, str):
        return None
    return content
