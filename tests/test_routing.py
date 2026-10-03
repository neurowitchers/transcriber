"""Tests for Telegram topic routing (Option B: single partition call).

Covers the three pieces of the routed-digest pipeline:

* ``build_agno_route_prompt`` — the partition prompt lists every configured
  topic (+ optional description) and the reserved default key, and demands
  strict JSON.
* ``_parse_route_json`` / ``_strip_json_fence`` — robust parsing of the model's
  JSON (fenced, missing keys, extra keys, non-string values, non-object).
* ``_disseminate_telegram`` (orchestrator) — routes each non-empty bucket to its
  chat, writes per-topic digest files, falls back on partition failure, and
  keeps the single-digest path when no routing is configured.

The model call itself is never made: ``build_routed_digests`` is monkeypatched
in the orchestrator tests, and the parser/prompt are pure functions.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from transcriber import __main__ as main_mod
from transcriber.agent import DEFAULT_ROUTE_KEY, build_agno_route_prompt
from transcriber.backends.errors import SummarizeError
from transcriber.backends.summarize_agno import (
    _parse_route_json,
    _strip_json_fence,
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


def make_config(
    recordings_dir: Path,
    *,
    routing: dict[str, str] | None = None,
    topic_descriptions: dict[str, str] | None = None,
) -> Config:
    return Config(
        recordings_dir=str(recordings_dir),
        stages=Stages(
            slides=SlidesStage(enabled=False, backend="openrouter"), s3_sync=False
        ),
        transcribe=Transcribe(model_id="microsoft/mai-transcribe-2"),
        summary=Summary(language="en", sections=["overview"], backend="agno"),
        agent=Agent(output_file="{basename}.md"),
        notion=Notion(
            server="n", parent_page_id="p", insert="subpage", token_env="NOTION_TOKEN"
        ),
        telegram=Telegram(
            bot_token_env="TG_TOKEN",
            default_chat_id="default-chat",
            routing=routing if routing is not None else {},
            topic_descriptions=topic_descriptions or {},
        ),
        timeouts=Timeouts(),
        openrouter=OpenRouter(api_key_env="OPENROUTER_API_KEY"),
    )


# --------------------------------------------------------------------------- #
# build_agno_route_prompt
# --------------------------------------------------------------------------- #
def test_route_prompt_lists_topics_and_default_key(tmp_path):
    cfg = make_config(tmp_path, routing={"ECL2.0": "-1", "MagicWheel": "-2"})
    prompt = build_agno_route_prompt(cfg, "# Summary\n\nStuff.")
    assert '"ECL2.0"' in prompt
    assert '"MagicWheel"' in prompt
    assert DEFAULT_ROUTE_KEY in prompt
    # Strict-JSON instruction present.
    assert "STRICT JSON" in prompt
    # The summary content is embedded.
    assert "Stuff." in prompt


def test_route_prompt_includes_descriptions_when_present(tmp_path):
    cfg = make_config(
        tmp_path,
        routing={"ECL2.0": "-1"},
        topic_descriptions={"ECL2.0": "the ECL 2.0 product line"},
    )
    prompt = build_agno_route_prompt(cfg, "summary")
    assert "the ECL 2.0 product line" in prompt


def test_route_prompt_omits_description_when_absent(tmp_path):
    cfg = make_config(tmp_path, routing={"ECL2.0": "-1"})
    prompt = build_agno_route_prompt(cfg, "summary")
    # Bare label line, no colon-description for the topic.
    assert '- "ECL2.0"' in prompt


# --------------------------------------------------------------------------- #
# _strip_json_fence
# --------------------------------------------------------------------------- #
def test_strip_fence_plain_json_unchanged():
    assert _strip_json_fence('{"a": "b"}') == '{"a": "b"}'


def test_strip_fence_removes_bare_fence():
    assert _strip_json_fence("```\n{\"a\": 1}\n```") == '{"a": 1}'


def test_strip_fence_removes_json_tagged_fence():
    assert _strip_json_fence("```json\n{\"a\": 1}\n```") == '{"a": 1}'


# --------------------------------------------------------------------------- #
# _parse_route_json
# --------------------------------------------------------------------------- #
def test_parse_fills_all_buckets(tmp_path):
    cfg = make_config(tmp_path, routing={"A": "-1", "B": "-2"})
    raw = '{"A": "alpha", "' + DEFAULT_ROUTE_KEY + '": "rest"}'
    out = _parse_route_json(raw, cfg)
    # Every configured topic + default key present, missing ones -> "".
    assert set(out) == {"A", "B", DEFAULT_ROUTE_KEY}
    assert out["A"] == "alpha"
    assert out["B"] == ""  # missing in payload -> empty
    assert out[DEFAULT_ROUTE_KEY] == "rest"


def test_parse_ignores_extra_keys(tmp_path):
    cfg = make_config(tmp_path, routing={"A": "-1"})
    raw = '{"A": "a", "' + DEFAULT_ROUTE_KEY + '": "d", "BOGUS": "x"}'
    out = _parse_route_json(raw, cfg)
    assert set(out) == {"A", DEFAULT_ROUTE_KEY}  # BOGUS dropped


def test_parse_coerces_non_string_values_to_empty(tmp_path):
    cfg = make_config(tmp_path, routing={"A": "-1"})
    raw = '{"A": 123, "' + DEFAULT_ROUTE_KEY + '": null}'
    out = _parse_route_json(raw, cfg)
    assert out["A"] == ""
    assert out[DEFAULT_ROUTE_KEY] == ""


def test_parse_handles_fenced_json(tmp_path):
    cfg = make_config(tmp_path, routing={"A": "-1"})
    raw = "```json\n{\"A\": \"a\", \"" + DEFAULT_ROUTE_KEY + "\": \"d\"}\n```"
    out = _parse_route_json(raw, cfg)
    assert out["A"] == "a"
    assert out[DEFAULT_ROUTE_KEY] == "d"


def test_parse_non_object_raises(tmp_path):
    cfg = make_config(tmp_path, routing={"A": "-1"})
    with pytest.raises(SummarizeError):
        _parse_route_json('["not", "an", "object"]', cfg)


def test_parse_non_json_raises(tmp_path):
    cfg = make_config(tmp_path, routing={"A": "-1"})
    with pytest.raises(SummarizeError):
        _parse_route_json("totally not json", cfg)


def test_parse_strips_values(tmp_path):
    cfg = make_config(tmp_path, routing={"A": "-1"})
    raw = '{"A": "  spaced  ", "' + DEFAULT_ROUTE_KEY + '": ""}'
    out = _parse_route_json(raw, cfg)
    assert out["A"] == "spaced"


# --------------------------------------------------------------------------- #
# _disseminate_telegram (orchestrator wiring)
# --------------------------------------------------------------------------- #
class _RecordingPublisher:
    """Captures (text, topic) per send; stands in for TelegramPublisher."""

    last_instance: "_RecordingPublisher | None" = None

    def __init__(self, config, client=None):
        self.config = config
        self.sends: list[tuple[str, object]] = []
        _RecordingPublisher.last_instance = self

    def send(self, text, topic=None):
        self.sends.append((text, topic))
        return []


def _write_summary(mp4: Path, config: Config, body: str = "# Summary\n\nBody.\n"):
    summary = main_mod._summary_path(mp4, config)
    summary.write_text(body, encoding="utf-8")
    return summary


def test_no_routing_sends_single_digest_to_default(monkeypatch, tmp_path):
    rec = tmp_path / "rec"
    rec.mkdir()
    mp4 = rec / "a.mp4"
    mp4.write_text("v", encoding="utf-8")
    config = make_config(rec, routing={})
    _write_summary(mp4, config)
    # digest present
    (rec / "a.telegram.md").write_text("digest body", encoding="utf-8")

    monkeypatch.setattr(main_mod, "TelegramPublisher", _RecordingPublisher)
    main_mod._disseminate_telegram(mp4, config, "a")

    pub = _RecordingPublisher.last_instance
    assert pub.sends == [("digest body", None)]  # single send, no topic


def test_routing_sends_each_nonempty_bucket(monkeypatch, tmp_path):
    rec = tmp_path / "rec"
    rec.mkdir()
    mp4 = rec / "a.mp4"
    mp4.write_text("v", encoding="utf-8")
    config = make_config(rec, routing={"ECL2.0": "-1", "MagicWheel": "-2"})
    summary = _write_summary(mp4, config)

    def fake_routed(cfg, summary_text):
        return {
            "ECL2.0": "ecl digest",
            "MagicWheel": "",  # empty -> skipped
            DEFAULT_ROUTE_KEY: "rest digest",
        }

    monkeypatch.setattr(main_mod, "TelegramPublisher", _RecordingPublisher)
    monkeypatch.setattr(
        "transcriber.backends.summarize_agno.build_routed_digests", fake_routed
    )
    main_mod._disseminate_telegram(mp4, config, "a")

    pub = _RecordingPublisher.last_instance
    # ECL2.0 routed by topic; MagicWheel skipped (empty); default sent topic=None.
    assert ("ecl digest", "ECL2.0") in pub.sends
    assert ("rest digest", None) in pub.sends
    assert all(t != "MagicWheel" for _txt, t in pub.sends)
    assert len(pub.sends) == 2

    # Per-topic digest files written (slug has dots replaced: ECL2.0 -> ECL2-0).
    assert (summary.parent / "a.telegram.ECL2-0.md").read_text(
        encoding="utf-8"
    ).strip() == "ecl digest"
    assert (summary.parent / "a.telegram.default.md").read_text(
        encoding="utf-8"
    ).strip() == "rest digest"
    # Empty bucket writes no file.
    assert not (summary.parent / "a.telegram.MagicWheel.md").exists()


def test_routing_partition_failure_falls_back_to_single_digest(monkeypatch, tmp_path):
    rec = tmp_path / "rec"
    rec.mkdir()
    mp4 = rec / "a.mp4"
    mp4.write_text("v", encoding="utf-8")
    config = make_config(rec, routing={"ECL2.0": "-1"})
    _write_summary(mp4, config)
    (rec / "a.telegram.md").write_text("fallback digest", encoding="utf-8")

    def boom(cfg, summary_text):
        raise SummarizeError("partition returned non-JSON output")

    monkeypatch.setattr(main_mod, "TelegramPublisher", _RecordingPublisher)
    monkeypatch.setattr(
        "transcriber.backends.summarize_agno.build_routed_digests", boom
    )
    main_mod._disseminate_telegram(mp4, config, "a")

    pub = _RecordingPublisher.last_instance
    # Fell back to the single digest to the default chat.
    assert pub.sends == [("fallback digest", None)]


def test_routing_all_empty_falls_back_to_single_digest(monkeypatch, tmp_path):
    rec = tmp_path / "rec"
    rec.mkdir()
    mp4 = rec / "a.mp4"
    mp4.write_text("v", encoding="utf-8")
    config = make_config(rec, routing={"ECL2.0": "-1"})
    _write_summary(mp4, config)
    (rec / "a.telegram.md").write_text("fallback digest", encoding="utf-8")

    def all_empty(cfg, summary_text):
        return {"ECL2.0": "", DEFAULT_ROUTE_KEY: ""}

    monkeypatch.setattr(main_mod, "TelegramPublisher", _RecordingPublisher)
    monkeypatch.setattr(
        "transcriber.backends.summarize_agno.build_routed_digests", all_empty
    )
    main_mod._disseminate_telegram(mp4, config, "a")

    pub = _RecordingPublisher.last_instance
    assert pub.sends == [("fallback digest", None)]
