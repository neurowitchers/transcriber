"""Guard against schema drift: the shipped example configs must load cleanly
under the Task 1 loader and produce a valid Config model."""

from __future__ import annotations

from pathlib import Path

import pytest

from transcriber.config import Config, load

EXAMPLES_DIR = Path(__file__).resolve().parent.parent / "examples"


@pytest.mark.parametrize(
    "filename",
    ["example.config.yaml", "acme.config.yaml"],
)
def test_example_config_loads(filename):
    path = EXAMPLES_DIR / filename
    assert path.is_file(), f"missing example config: {path}"

    cfg = load(str(path))
    assert isinstance(cfg, Config)


@pytest.mark.parametrize(
    "filename",
    ["example.config.yaml", "acme.config.yaml"],
)
def test_example_transcribe_is_openrouter_stt(filename):
    """model_id is an OpenRouter STT slug (vendor/model), `openrouter` is present,
    and the transcribe chunking/diarization fields parse (Task 6 / R18)."""
    cfg = load(str(EXAMPLES_DIR / filename))

    # `openrouter` is now unconditionally required (it is the transcriber).
    assert cfg.openrouter is not None
    assert cfg.openrouter.api_key_env == "OPENROUTER_API_KEY"

    # model_id is an OpenRouter slug (``vendor/model``), not the old ElevenLabs id.
    assert "/" in cfg.transcribe.model_id, cfg.transcribe.model_id
    assert cfg.transcribe.model_id == "microsoft/mai-transcribe-2"

    # Diarization + chunking fields parse to their expected example values.
    assert cfg.transcribe.diarize is True
    assert cfg.transcribe.segment_seconds == 480
    assert cfg.transcribe.overlap_seconds == 5


@pytest.mark.parametrize(
    "filename",
    ["example.config.yaml", "acme.config.yaml"],
)
def test_example_has_no_elevenlabs_or_scribe_references(filename):
    """No `scribe_v1` / `elevenlabs` strings remain in the example configs
    (grep-style assertion; case-insensitive) (Task 6 / R18)."""
    text = (EXAMPLES_DIR / filename).read_text(encoding="utf-8").lower()
    assert "scribe_v1" not in text
    assert "elevenlabs" not in text


def test_simple_example_shape():
    cfg = load(str(EXAMPLES_DIR / "example.config.yaml"))
    # Nested stages.slides shape parses into {enabled, backend}.
    assert cfg.stages.slides.enabled is True
    # openrouter is the only slides backend.
    assert cfg.stages.slides.backend == "openrouter"
    assert cfg.stages.s3_sync is False
    assert cfg.summary.language == "en"
    # Summarize runs on the agno backend (the only backend).
    assert cfg.summary.backend == "agno"
    assert cfg.notion.server == "notion-example"
    assert cfg.notion.insert == "subpage"
    # agno backend -> Notion REST token required.
    assert cfg.notion.token_env == "NOTION_API_KEY"
    assert cfg.telegram.bot_token_env == "TELEGRAM_BOT_TOKEN"
    assert cfg.telegram.routing == {}
    # No routing -> topic_descriptions defaults to an empty map.
    assert cfg.telegram.topic_descriptions == {}
    assert cfg.s3 is None
    # Slides are enabled, so the openrouter block is present (vision call).
    assert cfg.openrouter is not None
    assert cfg.openrouter.api_key_env == "OPENROUTER_API_KEY"
    assert cfg.openrouter.slides_model == "google/gemini-2.0-flash-001"


def test_full_example_shape():
    cfg = load(str(EXAMPLES_DIR / "acme.config.yaml"))
    # Nested stages.slides shape with the openrouter backend.
    assert cfg.stages.slides.enabled is True
    assert cfg.stages.slides.backend == "openrouter"
    assert cfg.stages.s3_sync is True
    assert cfg.summary.language == "original"
    # Summarize uses the agno backend.
    assert cfg.summary.backend == "agno"
    assert cfg.notion.server == "notion-acme"
    # notion.token_env present (required for summary.backend == "agno"),
    # referenced by env-var NAME only (no secret value).
    assert cfg.notion.token_env == "NOTION_API_KEY"
    assert cfg.telegram.bot_token_env == "ACME_BOT_TOKEN"
    assert cfg.telegram.routing == {
        "topic-a": "REPLACE_WITH_TOPIC_A_CHAT_ID",
        "topic-b": "REPLACE_WITH_TOPIC_B_CHAT_ID",
    }
    # Optional topic_descriptions (steers the routing partition call) parse per
    # topic; keys match the routing topics.
    assert set(cfg.telegram.topic_descriptions) == {"topic-a", "topic-b"}
    assert cfg.s3 is not None
    assert cfg.s3.profile == "acme"
    # openrouter block present and parses api_key_env + per-stage models;
    # base_url falls back to the default when omitted.
    assert cfg.openrouter is not None
    assert cfg.openrouter.api_key_env == "OPENROUTER_API_KEY"
    assert cfg.openrouter.base_url == "https://openrouter.ai/api/v1"
    assert cfg.openrouter.slides_model == "google/gemini-2.0-flash-001"
    assert cfg.openrouter.summary_model == "google/gemini-2.5-pro"
