"""Tests for transcriber.config: JSON/YAML parity, required-field errors,
and env-var resolution."""

from __future__ import annotations

import json

import pytest
import yaml

from transcriber.config import (
    Config,
    ConfigError,
    MissingEnvVarError,
    S3,
    load,
    resolve_env,
)


# A complete, valid config as a Python dict; serialized to both JSON and YAML
# to prove both formats map to the identical model.
BASE_CONFIG: dict = {
    "recordings_dir": "./recordings",
    "stages": {"slides": True, "s3_sync": False},
    "transcribe": {"model_id": "scribe_v1"},
    "summary": {"language": "en", "sections": ["overview", "action_items"]},
    "agent": {
        "cli": "kiro",
        "extra_args": ["--headless"],
        "output_file": "{basename}.md",
    },
    "notion": {
        "server": "notion-mcp",
        "parent_page_id": "abc123",
        "insert": "subpage",
    },
    "telegram": {
        "bot_token_env": "TELEGRAM_BOT_TOKEN",
        "default_chat_id": "-1001",
        "routing": {"topic-a": "-1002"},
    },
    "s3": {"bucket": "my-bucket", "profile": "default"},
    "timeouts": {
        "ffmpeg": 100,
        "scenedetect": 200,
        "elevenlabs": 300,
        "agy": 400,
        "s3": 500,
    },
}


def _write(path, data, fmt):
    if fmt == "json":
        path.write_text(json.dumps(data), encoding="utf-8")
    else:
        path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return str(path)


def test_json_and_yaml_parse_to_identical_model(tmp_path):
    json_path = _write(tmp_path / "config.json", BASE_CONFIG, "json")
    yaml_path = _write(tmp_path / "config.yaml", BASE_CONFIG, "yaml")

    cfg_json = load(json_path)
    cfg_yaml = load(yaml_path)

    assert isinstance(cfg_json, Config)
    assert cfg_json == cfg_yaml


def test_yml_extension_supported(tmp_path):
    yml_path = _write(tmp_path / "config.yml", BASE_CONFIG, "yaml")
    cfg = load(yml_path)
    assert cfg.recordings_dir == "./recordings"


def test_model_field_values(tmp_path):
    cfg = load(_write(tmp_path / "c.json", BASE_CONFIG, "json"))
    assert cfg.stages.slides is True
    assert cfg.transcribe.model_id == "scribe_v1"
    assert cfg.summary.language == "en"
    assert cfg.summary.sections == ["overview", "action_items"]
    assert cfg.agent.output_file == "{basename}.md"
    assert cfg.notion.insert == "subpage"
    assert cfg.telegram.bot_token_env == "TELEGRAM_BOT_TOKEN"
    assert cfg.telegram.routing == {"topic-a": "-1002"}
    assert isinstance(cfg.s3, S3)
    assert cfg.s3.bucket == "my-bucket"
    assert cfg.timeouts.ffmpeg == 100


def test_timeouts_default_when_omitted(tmp_path):
    data = dict(BASE_CONFIG)
    data.pop("timeouts")
    cfg = load(_write(tmp_path / "c.json", data, "json"))
    assert cfg.timeouts.ffmpeg == 900
    assert cfg.timeouts.scenedetect == 900
    assert cfg.timeouts.elevenlabs == 900
    assert cfg.timeouts.agy == 900
    assert cfg.timeouts.s3 == 900


def test_partial_timeouts_fill_defaults(tmp_path):
    data = json.loads(json.dumps(BASE_CONFIG))
    data["timeouts"] = {"ffmpeg": 42}
    cfg = load(_write(tmp_path / "c.json", data, "json"))
    assert cfg.timeouts.ffmpeg == 42
    assert cfg.timeouts.agy == 900


def test_s3_optional(tmp_path):
    data = dict(BASE_CONFIG)
    data.pop("s3")
    cfg = load(_write(tmp_path / "c.json", data, "json"))
    assert cfg.s3 is None


def test_missing_top_level_required_field(tmp_path):
    data = dict(BASE_CONFIG)
    data.pop("recordings_dir")
    path = _write(tmp_path / "c.json", data, "json")
    with pytest.raises(ConfigError) as exc:
        load(path)
    assert "recordings_dir" in str(exc.value)


def test_missing_nested_required_field(tmp_path):
    data = json.loads(json.dumps(BASE_CONFIG))
    del data["transcribe"]["model_id"]
    path = _write(tmp_path / "c.json", data, "json")
    with pytest.raises(ConfigError) as exc:
        load(path)
    assert "transcribe.model_id" in str(exc.value)


def test_invalid_summary_language(tmp_path):
    data = json.loads(json.dumps(BASE_CONFIG))
    data["summary"]["language"] = "fr"
    path = _write(tmp_path / "c.json", data, "json")
    with pytest.raises(ConfigError) as exc:
        load(path)
    assert "summary.language" in str(exc.value)


def test_unsupported_extension(tmp_path):
    path = tmp_path / "config.txt"
    path.write_text("nope", encoding="utf-8")
    with pytest.raises(ConfigError) as exc:
        load(str(path))
    assert "Unsupported" in str(exc.value)


def test_resolve_env_present(monkeypatch):
    monkeypatch.setenv("SOME_SECRET_TOKEN", "s3cr3t")
    assert resolve_env("SOME_SECRET_TOKEN") == "s3cr3t"


def test_resolve_env_absent_raises_readable_error(monkeypatch):
    monkeypatch.delenv("DEFINITELY_MISSING_VAR", raising=False)
    with pytest.raises(MissingEnvVarError) as exc:
        resolve_env("DEFINITELY_MISSING_VAR")
    assert "DEFINITELY_MISSING_VAR" in str(exc.value)


def test_secrets_not_stored_in_model(tmp_path, monkeypatch):
    # The model stores the env-var NAME, never the resolved secret value.
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "super-secret")
    cfg = load(_write(tmp_path / "c.json", BASE_CONFIG, "json"))
    assert cfg.telegram.bot_token_env == "TELEGRAM_BOT_TOKEN"
    assert "super-secret" not in repr(cfg)
