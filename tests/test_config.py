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
    OpenRouter,
    S3,
    SlidesStage,
    load,
    resolve_env,
)


# A complete, valid config as a Python dict; serialized to both JSON and YAML
# to prove both formats map to the identical model.
BASE_CONFIG: dict = {
    "recordings_dir": "./recordings",
    "stages": {"slides": {"enabled": True, "backend": "openrouter"}, "s3_sync": False},
    "transcribe": {"model_id": "microsoft/mai-transcribe-2"},
    "summary": {"language": "en", "sections": ["overview", "action_items"]},
    "agent": {
        "cli": "kiro",
        "extra_args": ["--headless"],
        "output_file": "{basename}.md",
    },
    "openrouter": {"api_key_env": "OPENROUTER_API_KEY"},
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
        "transcribe": 300,
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
    assert cfg.stages.slides.enabled is True
    assert cfg.stages.slides.backend == "openrouter"
    assert cfg.transcribe.model_id == "microsoft/mai-transcribe-2"
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
    assert cfg.timeouts.transcribe == 900
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
    del data["summary"]["language"]
    path = _write(tmp_path / "c.json", data, "json")
    with pytest.raises(ConfigError) as exc:
        load(path)
    assert "summary.language" in str(exc.value)


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



# --------------------------------------------------------------------------- #
# Per-stage backends, openrouter section, notion.token_env, new timeouts
# --------------------------------------------------------------------------- #
def test_slides_backend_defaults_to_openrouter_summary_to_agy(tmp_path):
    # Omitted slides backend defaults to "openrouter" (the only slides backend);
    # omitted summary backend defaults to "agy".
    data = json.loads(json.dumps(BASE_CONFIG))
    data["stages"]["slides"] = {"enabled": True}  # backend omitted
    # summary.backend omitted entirely
    cfg = load(_write(tmp_path / "c.json", data, "json"))
    assert cfg.stages.slides.backend == "openrouter"
    assert cfg.summary.backend == "agy"
    assert cfg.notion.token_env is None


def test_slides_openrouter_without_openrouter_section_raises(tmp_path):
    data = json.loads(json.dumps(BASE_CONFIG))
    data["stages"]["slides"] = {"enabled": True, "backend": "openrouter"}
    data.pop("openrouter", None)  # remove the required section
    with pytest.raises(ConfigError) as exc:
        load(_write(tmp_path / "c.json", data, "json"))
    assert "openrouter" in str(exc.value)


def test_summary_agno_without_openrouter_section_raises(tmp_path):
    data = json.loads(json.dumps(BASE_CONFIG))
    data["summary"]["backend"] = "agno"
    data["notion"]["token_env"] = "NOTION_TOKEN"  # present so this isn't the failing check
    # Slides must not require openrouter, and the section must be absent, so the
    # agno-openrouter check is the one that fires.
    data["stages"]["slides"] = {"enabled": False}
    data.pop("openrouter", None)
    with pytest.raises(ConfigError) as exc:
        load(_write(tmp_path / "c.json", data, "json"))
    assert "openrouter" in str(exc.value)


def test_summary_agno_without_notion_token_env_raises(tmp_path):
    data = json.loads(json.dumps(BASE_CONFIG))
    data["summary"]["backend"] = "agno"
    data["openrouter"] = {"api_key_env": "OPENROUTER_API_KEY"}
    with pytest.raises(ConfigError) as exc:
        load(_write(tmp_path / "c.json", data, "json"))
    assert "notion.token_env" in str(exc.value)


def test_summary_agno_valid_with_openrouter_and_token(tmp_path):
    data = json.loads(json.dumps(BASE_CONFIG))
    data["summary"]["backend"] = "agno"
    data["notion"]["token_env"] = "NOTION_TOKEN"
    data["openrouter"] = {"api_key_env": "OPENROUTER_API_KEY"}
    cfg = load(_write(tmp_path / "c.json", data, "json"))
    assert cfg.summary.backend == "agno"
    assert cfg.notion.token_env == "NOTION_TOKEN"
    assert cfg.openrouter.summary_model == "google/gemini-2.5-pro"


def test_invalid_slides_backend_raises_with_allowed_set(tmp_path):
    data = json.loads(json.dumps(BASE_CONFIG))
    # "agy" is no longer a valid slides backend (openrouter is the only one).
    data["stages"]["slides"] = {"enabled": True, "backend": "agy"}
    with pytest.raises(ConfigError) as exc:
        load(_write(tmp_path / "c.json", data, "json"))
    msg = str(exc.value)
    assert "stages.slides.backend" in msg
    assert "openrouter" in msg


def test_invalid_summary_backend_raises_with_allowed_set(tmp_path):
    data = json.loads(json.dumps(BASE_CONFIG))
    # "openrouter" is valid for slides but NOT for summary.
    data["summary"]["backend"] = "openrouter"
    with pytest.raises(ConfigError) as exc:
        load(_write(tmp_path / "c.json", data, "json"))
    msg = str(exc.value)
    assert "summary.backend" in msg
    assert "agy" in msg and "agno" in msg


def test_openrouter_defaults(tmp_path):
    data = json.loads(json.dumps(BASE_CONFIG))
    data["stages"]["slides"] = {"enabled": True, "backend": "openrouter"}
    data["openrouter"] = {"api_key_env": "OPENROUTER_API_KEY"}
    cfg = load(_write(tmp_path / "c.json", data, "json"))
    assert isinstance(cfg.openrouter, OpenRouter)
    assert cfg.openrouter.api_key_env == "OPENROUTER_API_KEY"
    assert cfg.openrouter.base_url == "https://openrouter.ai/api/v1"
    assert cfg.openrouter.slides_model == "google/gemini-2.0-flash-001"
    assert cfg.openrouter.summary_model == "google/gemini-2.5-pro"


def test_openrouter_overrides(tmp_path):
    data = json.loads(json.dumps(BASE_CONFIG))
    data["stages"]["slides"] = {"enabled": True, "backend": "openrouter"}
    data["openrouter"] = {
        "api_key_env": "OPENROUTER_API_KEY",
        "base_url": "https://example.test/v1",
        "slides_model": "vendor/vision",
        "summary_model": "vendor/strong",
    }
    cfg = load(_write(tmp_path / "c.json", data, "json"))
    assert cfg.openrouter.base_url == "https://example.test/v1"
    assert cfg.openrouter.slides_model == "vendor/vision"
    assert cfg.openrouter.summary_model == "vendor/strong"


def test_legacy_bool_slides_true_raises(tmp_path):
    data = json.loads(json.dumps(BASE_CONFIG))
    data["stages"]["slides"] = True
    with pytest.raises(ConfigError) as exc:
        load(_write(tmp_path / "c.json", data, "json"))
    assert "stages.slides" in str(exc.value)


def test_legacy_bool_slides_false_raises(tmp_path):
    data = json.loads(json.dumps(BASE_CONFIG))
    data["stages"]["slides"] = False
    with pytest.raises(ConfigError) as exc:
        load(_write(tmp_path / "c.json", data, "json"))
    assert "stages.slides" in str(exc.value)


def test_timeouts_slides_default_and_summarize_none(tmp_path):
    data = json.loads(json.dumps(BASE_CONFIG))
    # BASE_CONFIG.timeouts does not set slides/summarize.
    cfg = load(_write(tmp_path / "c.json", data, "json"))
    assert cfg.timeouts.slides == 900
    assert cfg.timeouts.summarize is None


def test_timeouts_slides_and_summarize_overridable(tmp_path):
    data = json.loads(json.dumps(BASE_CONFIG))
    data["timeouts"]["slides"] = 123
    data["timeouts"]["summarize"] = 456
    cfg = load(_write(tmp_path / "c.json", data, "json"))
    assert cfg.timeouts.slides == 123
    assert cfg.timeouts.summarize == 456


def test_slides_stage_is_dataclass(tmp_path):
    cfg = load(_write(tmp_path / "c.json", BASE_CONFIG, "json"))
    assert isinstance(cfg.stages.slides, SlidesStage)


# --------------------------------------------------------------------------- #
# OpenRouter transcription: mandatory openrouter, transcribe defaults, timeouts
# --------------------------------------------------------------------------- #
def test_openrouter_unconditionally_required(tmp_path):
    # A config without an `openrouter` section always raises now (R13), even
    # with slides disabled and summary on the default `agy` backend.
    data = json.loads(json.dumps(BASE_CONFIG))
    data["stages"]["slides"] = {"enabled": False}
    data["summary"]["backend"] = "agy"
    data.pop("openrouter", None)
    with pytest.raises(ConfigError) as exc:
        load(_write(tmp_path / "c.json", data, "json"))
    assert "openrouter" in str(exc.value)


def test_transcribe_defaults_populate(tmp_path):
    # Only model_id provided; the new fields fall back to their defaults.
    data = json.loads(json.dumps(BASE_CONFIG))
    data["transcribe"] = {"model_id": "microsoft/mai-transcribe-2"}
    cfg = load(_write(tmp_path / "c.json", data, "json"))
    assert cfg.transcribe.model_id == "microsoft/mai-transcribe-2"
    assert cfg.transcribe.diarize is True
    assert cfg.transcribe.segment_seconds == 480
    assert cfg.transcribe.overlap_seconds == 5


def test_transcribe_model_id_default_when_omitted(tmp_path):
    # model_id itself now has a default (the OpenRouter STT slug).
    data = json.loads(json.dumps(BASE_CONFIG))
    data["transcribe"] = {}
    cfg = load(_write(tmp_path / "c.json", data, "json"))
    assert cfg.transcribe.model_id == "microsoft/mai-transcribe-2"


def test_legacy_elevenlabs_timeout_silently_ignored(tmp_path):
    # Old host configs carrying `timeouts.elevenlabs` still load; the value is
    # dropped and `timeouts.transcribe` defaults to 900 (R15).
    data = json.loads(json.dumps(BASE_CONFIG))
    data["timeouts"] = {"ffmpeg": 42, "elevenlabs": 12345}
    cfg = load(_write(tmp_path / "c.json", data, "json"))
    assert cfg.timeouts.ffmpeg == 42
    assert cfg.timeouts.transcribe == 900
    assert not hasattr(cfg.timeouts, "elevenlabs")


def test_timeouts_transcribe_default_and_overridable(tmp_path):
    data = json.loads(json.dumps(BASE_CONFIG))
    data["timeouts"] = {}
    cfg = load(_write(tmp_path / "c.json", data, "json"))
    assert cfg.timeouts.transcribe == 900

    data["timeouts"] = {"transcribe": 777}
    cfg = load(_write(tmp_path / "c2.json", data, "json"))
    assert cfg.timeouts.transcribe == 777


def test_transcribe_fields_round_trip_json_and_yaml(tmp_path):
    data = json.loads(json.dumps(BASE_CONFIG))
    data["transcribe"] = {
        "model_id": "vendor/stt-slug",
        "diarize": False,
        "segment_seconds": 300,
        "overlap_seconds": 10,
    }
    cfg_json = load(_write(tmp_path / "c.json", data, "json"))
    cfg_yaml = load(_write(tmp_path / "c.yaml", data, "yaml"))

    assert cfg_json == cfg_yaml
    for cfg in (cfg_json, cfg_yaml):
        assert cfg.transcribe.model_id == "vendor/stt-slug"
        assert cfg.transcribe.diarize is False
        assert cfg.transcribe.segment_seconds == 300
        assert cfg.transcribe.overlap_seconds == 10
