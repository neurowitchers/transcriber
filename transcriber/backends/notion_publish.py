"""Direct Notion REST publish for the ``agno`` summarize backend.

The ``agno`` backend produces the Markdown summary with an OpenRouter model, and
then **the engine** publishes it to Notion by calling the Notion REST API
directly — no MCP server, no model tool-calling. This is deterministic and
avoids the Notion-MCP-over-OpenRouter tool-schema incompatibility (Gemini and
other models return an empty ``null`` completion when handed the official Notion
MCP's ``oneOf``/``anyOf``/``$ref`` tool schemas).

What it does
------------
1. Extract the parent **page id** from ``notion.parent_page_id`` (accepts either
   a bare id — hyphenated or not — or a full Notion URL with the id embedded).
2. Convert the Markdown summary into Notion **block** objects, **losing
   nothing**: headings, paragraphs, bullet/numbered list items, blockquotes and
   fenced code blocks map to their native block types; anything else falls back
   to a paragraph block so all text survives.
3. Create a new subpage under the parent (title = the recording basename) with
   up to the first 100 blocks, then append the remainder in <=100-block chunks
   (Notion caps children per request at 100).
4. When cleaned slide descriptions are supplied, create a child page titled
   "Slide Descriptions" **under the summary page** holding the slide blocks, so
   the summary page stays short and the long slide detail lives one click away.

Secrets: the token is resolved by env-var *name* (``notion.token_env``) at use
time and is **never logged**.
"""

from __future__ import annotations

import json
import logging
import re
import urllib.error
import urllib.request
from typing import Any, Optional

from transcriber.backends.errors import SummarizeError
from transcriber.config import Config, resolve_env

logger = logging.getLogger("transcriber.backends.notion_publish")

__all__ = [
    "extract_page_id",
    "markdown_to_blocks",
    "publish_to_notion",
    "SLIDES_SUBPAGE_TITLE",
    "NOTION_VERSION",
    "DEFAULT_NOTION_TIMEOUT",
]

#: Notion API version pinned for stable block/page shapes.
NOTION_VERSION = "2022-06-28"

#: Notion caps the number of block children accepted per request.
_MAX_CHILDREN_PER_REQUEST = 100

#: Notion rejects rich-text content longer than 2000 chars per text object.
_MAX_TEXT_LEN = 2000

#: Default bounded per-request timeout (seconds) for Notion REST calls. Without
#: this, ``urlopen`` blocks indefinitely; because the publish runs inside
#: ``asyncio.to_thread``, cancelling the outer ``wait_for`` cannot stop a stuck
#: urllib worker, so ``timeouts.summarize`` would not be a hard bound for the
#: Notion phase. A bounded socket timeout makes each request fail fast instead.
DEFAULT_NOTION_TIMEOUT = 60.0

_UUID_RE = re.compile(r"([0-9a-fA-F]{32})")
_HYPHENATED_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)


def extract_page_id(value: str) -> str:
    """Return a hyphenated Notion page id from an id or a Notion URL.

    Accepts a bare 32-hex id, an already-hyphenated UUID, or a full Notion URL
    (``https://app.notion.com/.../Title-<32hex>``). Raises ``SummarizeError`` if
    no id can be found.
    """
    if not value:
        raise SummarizeError("notion.parent_page_id is empty")

    # Already hyphenated UUID somewhere in the string.
    m = _HYPHENATED_RE.search(value)
    if m:
        return m.group(0).lower()

    # Otherwise find a bare 32-hex run (id or trailing URL segment).
    m = _UUID_RE.search(value.replace("-", ""))
    if not m:
        raise SummarizeError(
            "could not extract a Notion page id from notion.parent_page_id "
            "(expected a 32-char id or a Notion URL containing one)"
        )
    h = m.group(1).lower()
    return f"{h[0:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:]}"


def _rich_text(text: str) -> list[dict[str, Any]]:
    """Build a Notion rich_text array, chunking to the per-object length cap."""
    if not text:
        return []
    chunks = [
        text[i : i + _MAX_TEXT_LEN] for i in range(0, len(text), _MAX_TEXT_LEN)
    ]
    return [{"type": "text", "text": {"content": c}} for c in chunks]


def _block(block_type: str, text: str) -> dict[str, Any]:
    return {
        "object": "block",
        "type": block_type,
        block_type: {"rich_text": _rich_text(text)},
    }


def markdown_to_blocks(markdown: str) -> list[dict[str, Any]]:
    """Convert a Markdown summary into Notion block objects, losing nothing.

    Supported mappings (line-oriented, which matches our summaries):

    * ``#``/``##``/``###`` -> ``heading_1``/``heading_2``/``heading_3``
      (Notion has no heading_4+, so deeper headings clamp to ``heading_3``).
    * ``-``/``*``/``+`` -> ``bulleted_list_item``.
    * ``1.`` etc. -> ``numbered_list_item``.
    * ``>`` -> ``quote``.
    * ```` ``` ```` fenced blocks -> a single ``code`` block (content verbatim).
    * Blank line -> skipped (Notion spaces blocks itself).
    * Anything else -> ``paragraph`` (nothing is dropped).

    Inline markers (``**bold**`` etc.) are kept as literal text so no characters
    are lost; we intentionally do not strip them.
    """
    blocks: list[dict[str, Any]] = []
    lines = markdown.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        # Fenced code block: collect until the closing fence (or EOF).
        if stripped.startswith("```"):
            fence = stripped[:3]
            code_lines: list[str] = []
            i += 1
            while i < len(lines) and lines[i].strip() != fence:
                code_lines.append(lines[i])
                i += 1
            i += 1  # skip the closing fence (no-op past EOF)
            blocks.append(
                {
                    "object": "block",
                    "type": "code",
                    "code": {
                        "rich_text": _rich_text("\n".join(code_lines)),
                        "language": "plain text",
                    },
                }
            )
            continue

        if not stripped:
            i += 1
            continue

        heading = re.match(r"^(#{1,6})\s+(.*)$", stripped)
        if heading:
            level = min(len(heading.group(1)), 3)
            blocks.append(_block(f"heading_{level}", heading.group(2)))
            i += 1
            continue

        bullet = re.match(r"^[-*+]\s+(.*)$", stripped)
        if bullet:
            blocks.append(_block("bulleted_list_item", bullet.group(1)))
            i += 1
            continue

        numbered = re.match(r"^\d+[.)]\s+(.*)$", stripped)
        if numbered:
            blocks.append(_block("numbered_list_item", numbered.group(1)))
            i += 1
            continue

        quote = re.match(r"^>\s?(.*)$", stripped)
        if quote:
            blocks.append(_block("quote", quote.group(1)))
            i += 1
            continue

        # Fallback: keep the full line as a paragraph (lose nothing).
        blocks.append(_block("paragraph", stripped))
        i += 1

    return blocks


def _notion_request(
    url: str,
    token: str,
    payload: Optional[dict],
    method: str,
    *,
    timeout: float = DEFAULT_NOTION_TIMEOUT,
) -> dict[str, Any]:
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Notion-Version": NOTION_VERSION,
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        # Body may carry a Notion error code but never the token (not echoed).
        body = exc.read().decode(errors="replace")[:300]
        raise SummarizeError(
            f"Notion API {method} {url.rsplit('/', 1)[-1]} failed: "
            f"HTTP {exc.code} {body}"
        ) from exc
    except TimeoutError as exc:
        # Bounded socket timeout tripped (urllib raises socket.timeout, an
        # alias of the builtin TimeoutError on 3.10+). Surface as a retryable
        # SummarizeError so the stage fails fast rather than hanging.
        raise SummarizeError(
            f"Notion API {method} {url.rsplit('/', 1)[-1]} timed out "
            f"after {timeout:.0f}s"
        ) from exc
    except urllib.error.URLError as exc:
        # A socket timeout can also surface wrapped in URLError.
        if isinstance(exc.reason, TimeoutError):
            raise SummarizeError(
                f"Notion API {method} {url.rsplit('/', 1)[-1]} timed out "
                f"after {timeout:.0f}s"
            ) from exc
        raise SummarizeError(f"Notion API request failed: {exc.reason}") from exc


def _create_page(
    token: str,
    parent: dict[str, Any],
    title: str,
    blocks: list[dict[str, Any]],
    *,
    timeout: float,
) -> tuple[str, str]:
    """Create a Notion page under ``parent`` with ``blocks``; return (id, url).

    ``parent`` is a Notion parent object — ``{"page_id": ...}`` for a subpage
    under a page. Blocks beyond Notion's 100-per-request cap are appended in
    follow-up PATCH requests. Raises ``SummarizeError`` on any failure.
    """
    first, rest = (
        blocks[:_MAX_CHILDREN_PER_REQUEST],
        blocks[_MAX_CHILDREN_PER_REQUEST:],
    )
    create_payload = {
        "parent": parent,
        "properties": {"title": {"title": [{"text": {"content": title}}]}},
        "children": first,
    }
    page = _notion_request(
        "https://api.notion.com/v1/pages",
        token,
        create_payload,
        "POST",
        timeout=timeout,
    )
    page_id = page.get("id")
    page_url = page.get("url", "")
    if not page_id:
        raise SummarizeError("Notion create-page returned no page id")

    for start in range(0, len(rest), _MAX_CHILDREN_PER_REQUEST):
        chunk = rest[start : start + _MAX_CHILDREN_PER_REQUEST]
        _notion_request(
            f"https://api.notion.com/v1/blocks/{page_id}/children",
            token,
            {"children": chunk},
            "PATCH",
            timeout=timeout,
        )
    return page_id, page_url


#: Title of the child page that holds the slide descriptions.
SLIDES_SUBPAGE_TITLE = "Slide Descriptions"


def publish_to_notion(
    config: Config,
    title: str,
    summary_markdown: str,
    *,
    slides_markdown: Optional[str] = None,
    timeout: float = DEFAULT_NOTION_TIMEOUT,
) -> str:
    """Create a Notion subpage with the summary; optionally a slides child page.

    Creates a new page titled ``title`` under ``notion.parent_page_id`` holding
    the **summary** blocks. When ``slides_markdown`` is a non-empty block of
    cleaned slide descriptions, a child page titled
    :data:`SLIDES_SUBPAGE_TITLE` is then created **under the summary page** with
    the slide blocks — so the summary page stays readable and the (often long)
    slide descriptions live one click away. Returns the **summary** page URL.

    ``slides_markdown`` must already be the reader-facing, markers-stripped slide
    text (``<name>.slides-clean.md``); this function does not strip markers.
    ``None``/empty/whitespace → no slides child page is created.

    Raises ``SummarizeError`` on any failure so the summarize stage fails
    (retryable). Note: if the summary page is created but the slides child page
    then fails, the whole call raises; the caller must not record a successful
    publish, so a retry re-creates both (a narrow duplicate-summary-page window,
    unchanged in risk from the single-page design).

    ``timeout`` bounds each individual REST request (seconds). Because this runs
    in a worker thread under the outer summarize ``wait_for``, a bounded socket
    timeout is what actually caps the Notion phase.
    """
    if not config.notion.token_env:
        raise SummarizeError(
            "notion.token_env is required for summary.backend == 'agno'"
        )
    token = resolve_env(config.notion.token_env)
    parent_id = extract_page_id(config.notion.parent_page_id)

    blocks = markdown_to_blocks(summary_markdown)
    if not blocks:
        raise SummarizeError("summary produced no Notion blocks to publish")

    logger.info(
        "notion publish: creating summary subpage title=%r under parent "
        "(blocks=%d)",
        title,
        len(blocks),
    )
    page_id, page_url = _create_page(
        token, {"page_id": parent_id}, title, blocks, timeout=timeout
    )

    # Optional slides child page under the just-created summary page.
    if slides_markdown and slides_markdown.strip():
        slide_blocks = markdown_to_blocks(slides_markdown)
        if slide_blocks:
            logger.info(
                "notion publish: creating %r child page under summary "
                "(blocks=%d)",
                SLIDES_SUBPAGE_TITLE,
                len(slide_blocks),
            )
            _create_page(
                token,
                {"page_id": page_id},
                SLIDES_SUBPAGE_TITLE,
                slide_blocks,
                timeout=timeout,
            )

    logger.info("notion publish: created summary page url=%s", page_url)
    return page_url
