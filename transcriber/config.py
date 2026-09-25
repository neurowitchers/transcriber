"""Configuration model and loader for the transcriber.

Loads a config file (``.json`` / ``.yaml`` / ``.yml``) into a single validated
dataclass model. Both formats map to the identical :class:`Config` model.

Secrets are never stored in the model. The config references secrets by
environment-variable **name** only (e.g. ``telegram.bot_token_env``); the actual
value is resolved at *use* time via :func:`resolve_env`.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, fields
from typing import Any, Optional

try:
    import yaml
except ImportError:  # pragma: no cover - PyYAML is a declared dependency
    yaml = None  # type: ignore[assignment]


# Default timeout (seconds) applied to any omitted timeout field.
DEFAULT_TIMEOUT_SECONDS = 900

# Allowed per-stage backend selectors.
SLIDES_BACKENDS = ("agy", "openrouter")
SUMMARY_BACKENDS = ("agy", "agno")


class ConfigError(ValueError):
    """Raised when a config file is invalid (missing/invalid required fields)."""


class MissingEnvVarError(RuntimeError):
    """Raised when a referenced environment variable is absent at use time."""


# --------------------------------------------------------------------------- #
# Model
# --------------------------------------------------------------------------- #
@dataclass
class SlidesStage:
    enabled: bool  # was stages.slides (bare bool); now nested.
    backend: str = "agy"  # "agy" | "openrouter"


@dataclass
class Stages:
    slides: SlidesStage
    s3_sync: bool


@dataclass
class Transcribe:
    model_id: str


@dataclass
class Summary:
    language: str  # "en" | "original"
    sections: list[str]
    backend: str = "agy"  # "agy" | "agno"


@dataclass
class Agent:
    cli: str
    extra_args: list[str]
    output_file: str  # templated with {basename}


@dataclass
class OpenRouter:
    api_key_env: str  # env-var NAME for the OpenRouter API key.
    base_url: str = "https://openrouter.ai/api/v1"
    slides_model: str = "google/gemini-2.0-flash-001"  # vision (slides call)
    summary_model: str = "google/gemini-2.5-pro"  # tool-capable (agno + MCP)
    # Deterministic slide-count ceiling for the `openrouter` slides backend.
    # An over-ceiling deck hard-fails before any image is sent (cost guard).
    # Optional; defaults to 60 when unset.
    max_slides: int = 60
    # Slides are described in batches of this many images per vision call, so a
    # large deck never overflows the model's context window. The per-batch
    # markdown is concatenated. Optional; defaults to 20 when unset.
    slides_batch_size: int = 20


@dataclass
class Notion:
    server: str
    parent_page_id: str
    insert: str  # e.g. "subpage"
    # env-var NAME for the Notion API key. The engine launches the Notion MCP
    # with this key. Required when summary.backend == "agno".
    token_env: Optional[str] = None


@dataclass
class Telegram:
    bot_token_env: str
    default_chat_id: str
    routing: dict[str, str]


@dataclass
class S3:
    bucket: str
    profile: str


@dataclass
class Timeouts:
    ffmpeg: int = DEFAULT_TIMEOUT_SECONDS
    scenedetect: int = DEFAULT_TIMEOUT_SECONDS
    slides: int = DEFAULT_TIMEOUT_SECONDS  # describe_slides stage
    elevenlabs: int = DEFAULT_TIMEOUT_SECONDS
    agy: int = DEFAULT_TIMEOUT_SECONDS
    # summarize-stage timeout; None falls back to `agy` at use time.
    summarize: Optional[int] = None
    s3: int = DEFAULT_TIMEOUT_SECONDS


@dataclass
class Config:
    recordings_dir: str
    stages: Stages
    transcribe: Transcribe
    summary: Summary
    agent: Agent
    notion: Notion
    telegram: Telegram
    timeouts: Timeouts
    s3: Optional[S3] = None
    openrouter: Optional[OpenRouter] = None


# --------------------------------------------------------------------------- #
# Validation helpers
# --------------------------------------------------------------------------- #
def _require_mapping(value: Any, path: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ConfigError(
            f"'{path}' must be a mapping/object, got {type(value).__name__}"
        )
    return value


def _get_required(mapping: dict[str, Any], key: str, path: str) -> Any:
    if key not in mapping:
        full = f"{path}.{key}" if path else key
        raise ConfigError(f"Missing required config field: '{full}'")
    return mapping[key]


def _build_section(cls: Any, data: Any, path: str) -> Any:
    """Build a required dataclass section from ``data`` (a mapping)."""
    mapping = _require_mapping(data, path)
    kwargs: dict[str, Any] = {}
    for f in fields(cls):
        full = f"{path}.{f.name}" if path else f.name
        if f.name in mapping:
            kwargs[f.name] = mapping[f.name]
        elif _has_default(f):
            # Rely on the dataclass default (e.g. Timeouts).
            continue
        else:
            raise ConfigError(f"Missing required config field: '{full}'")
    return cls(**kwargs)


def _has_default(f: Any) -> bool:
    from dataclasses import MISSING

    return f.default is not MISSING or f.default_factory is not MISSING  # type: ignore[misc]


# --------------------------------------------------------------------------- #
# Loader
# --------------------------------------------------------------------------- #
def _parse_file(path: str) -> dict[str, Any]:
    ext = os.path.splitext(path)[1].lower()
    with open(path, "r", encoding="utf-8") as fh:
        text = fh.read()

    if ext == ".json":
        data = json.loads(text)
    elif ext in (".yaml", ".yml"):
        if yaml is None:  # pragma: no cover
            raise ConfigError("PyYAML is required to load YAML config files")
        data = yaml.safe_load(text)
    else:
        raise ConfigError(
            f"Unsupported config extension '{ext}' for '{path}'. "
            "Expected one of: .json, .yaml, .yml"
        )

    if not isinstance(data, dict):
        raise ConfigError(f"Config root of '{path}' must be a mapping/object")
    return data


def _from_dict(data: dict[str, Any]) -> Config:
    """Build a validated :class:`Config` from a raw mapping."""
    recordings_dir = _get_required(data, "recordings_dir", "")

    stages_data = _require_mapping(_get_required(data, "stages", ""), "stages")
    slides_raw = _get_required(stages_data, "slides", "stages")
    if not isinstance(slides_raw, dict):
        raise ConfigError(
            "'stages.slides' must be a mapping with keys "
            "{enabled, backend}; the legacy bare boolean form is not "
            f"accepted, got {type(slides_raw).__name__}"
        )
    slides_stage = _build_section(SlidesStage, slides_raw, "stages.slides")
    s3_sync = _get_required(stages_data, "s3_sync", "stages")
    stages = Stages(slides=slides_stage, s3_sync=s3_sync)

    transcribe = _build_section(
        Transcribe, _get_required(data, "transcribe", ""), "transcribe"
    )
    summary = _build_section(Summary, _get_required(data, "summary", ""), "summary")
    agent = _build_section(Agent, _get_required(data, "agent", ""), "agent")
    notion = _build_section(Notion, _get_required(data, "notion", ""), "notion")
    telegram = _build_section(
        Telegram, _get_required(data, "telegram", ""), "telegram"
    )

    # timeouts: optional as a whole; each field defaults to DEFAULT_TIMEOUT_SECONDS.
    timeouts = _build_section(Timeouts, data.get("timeouts", {}), "timeouts")

    # s3: optional.
    s3_data = data.get("s3")
    s3 = _build_section(S3, s3_data, "s3") if s3_data is not None else None

    # openrouter: optional (present-or-None, like s3).
    openrouter_data = data.get("openrouter")
    openrouter = (
        _build_section(OpenRouter, openrouter_data, "openrouter")
        if openrouter_data is not None
        else None
    )

    # Validate constrained values.
    if summary.language not in ("en", "original"):
        raise ConfigError(
            "'summary.language' must be 'en' or 'original', "
            f"got {summary.language!r}"
        )

    if stages.slides.backend not in SLIDES_BACKENDS:
        raise ConfigError(
            "'stages.slides.backend' must be one of "
            f"{SLIDES_BACKENDS!r}, got {stages.slides.backend!r}"
        )
    if summary.backend not in SUMMARY_BACKENDS:
        raise ConfigError(
            "'summary.backend' must be one of "
            f"{SUMMARY_BACKENDS!r}, got {summary.backend!r}"
        )

    # openrouter required iff slides use openrouter or summary uses agno.
    needs_openrouter = (
        stages.slides.backend == "openrouter" or summary.backend == "agno"
    )
    if needs_openrouter and openrouter is None:
        raise ConfigError(
            "'openrouter' section is required when "
            "stages.slides.backend == 'openrouter' or summary.backend == 'agno'"
        )

    # notion.token_env required iff summary uses agno.
    if summary.backend == "agno" and not notion.token_env:
        raise ConfigError(
            "'notion.token_env' is required when summary.backend == 'agno'"
        )

    return Config(
        recordings_dir=recordings_dir,
        stages=stages,
        transcribe=transcribe,
        summary=summary,
        agent=agent,
        notion=notion,
        telegram=telegram,
        timeouts=timeouts,
        s3=s3,
        openrouter=openrouter,
    )


def load(path: str) -> Config:
    """Load and validate a config file into a :class:`Config` model.

    The format is selected by file extension:
    ``.json`` -> JSON, ``.yaml`` / ``.yml`` -> YAML. Both map to the identical
    model.

    Raises:
        ConfigError: if the file is unreadable, has an unsupported extension,
            or is missing a required field / has an invalid value.
    """
    data = _parse_file(path)
    return _from_dict(data)


# --------------------------------------------------------------------------- #
# Secret resolution (at use time)
# --------------------------------------------------------------------------- #
def resolve_env(var_name: str) -> str:
    """Resolve an environment variable by name at *use* time.

    Args:
        var_name: The environment variable name (as referenced in config,
            e.g. ``telegram.bot_token_env``'s value).

    Returns:
        The environment variable's value.

    Raises:
        MissingEnvVarError: if the variable is not set, with a readable message
            naming the variable.
    """
    value = os.environ.get(var_name)
    if value is None:
        raise MissingEnvVarError(
            f"Required environment variable '{var_name}' is not set"
        )
    return value
