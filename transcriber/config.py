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


class ConfigError(ValueError):
    """Raised when a config file is invalid (missing/invalid required fields)."""


class MissingEnvVarError(RuntimeError):
    """Raised when a referenced environment variable is absent at use time."""


# --------------------------------------------------------------------------- #
# Model
# --------------------------------------------------------------------------- #
@dataclass
class Stages:
    slides: bool
    s3_sync: bool


@dataclass
class Transcribe:
    model_id: str


@dataclass
class Summary:
    language: str  # "en" | "original"
    sections: list[str]


@dataclass
class Agent:
    cli: str
    extra_args: list[str]
    output_file: str  # templated with {basename}


@dataclass
class Notion:
    server: str
    parent_page_id: str
    insert: str  # e.g. "subpage"


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
    elevenlabs: int = DEFAULT_TIMEOUT_SECONDS
    agy: int = DEFAULT_TIMEOUT_SECONDS
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

    stages = _build_section(Stages, _get_required(data, "stages", ""), "stages")
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

    # Validate constrained values.
    if summary.language not in ("en", "original"):
        raise ConfigError(
            "'summary.language' must be 'en' or 'original', "
            f"got {summary.language!r}"
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
