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


def test_simple_example_shape():
    cfg = load(str(EXAMPLES_DIR / "example.config.yaml"))
    assert cfg.stages.slides is True
    assert cfg.stages.parse_transcript is True
    assert cfg.stages.s3_sync is False
    assert cfg.summary.language == "en"
    assert cfg.notion.server == "notion-example"
    assert cfg.notion.insert == "subpage"
    assert cfg.telegram.bot_token_env == "TELEGRAM_BOT_TOKEN"
    assert cfg.telegram.routing == {}
    assert cfg.s3 is None


def test_full_example_shape():
    cfg = load(str(EXAMPLES_DIR / "acme.config.yaml"))
    assert cfg.stages.s3_sync is True
    assert cfg.summary.language == "original"
    assert cfg.notion.server == "notion-acme"
    assert cfg.telegram.bot_token_env == "ACME_BOT_TOKEN"
    assert cfg.telegram.routing == {
        "topic-a": "REPLACE_WITH_TOPIC_A_CHAT_ID",
        "topic-b": "REPLACE_WITH_TOPIC_B_CHAT_ID",
    }
    assert cfg.s3 is not None
    assert cfg.s3.profile == "acme"
