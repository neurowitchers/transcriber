"""Tests for config auto-discovery (``resolve_config_path``) and the resolved
config path surfaced in ``--dry-run`` / ``check`` and logged on every run.

Covers the installable-tool spec (R1–R5b) and the critique's E7 matrix:
precedence, argument ordering (P5), strict authoritative sources with no CWD
fallback (E3), directory/non-file rejection (E5), empty env treated as unset,
candidate shadowing notice (P6), and the INFO ``using config:`` line (E8).
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from transcriber import __main__ as main_mod
from transcriber.config import ConfigNotFoundError


# --------------------------------------------------------------------------- #
# Pure-function resolver tests (no main(), no real env/CWD)
# --------------------------------------------------------------------------- #
def _touch(p: Path) -> Path:
    p.write_text("recordings_dir: ./rec\n", encoding="utf-8")
    return p


def test_explicit_config_wins_over_env_and_cwd(tmp_path):
    explicit = _touch(tmp_path / "explicit.yaml")
    _touch(tmp_path / "transcriber.config.yaml")
    _touch(tmp_path / "config.yaml")
    env = {"TRANSCRIBER_CONFIG": str(_touch(tmp_path / "env.yaml"))}
    assert main_mod.resolve_config_path(str(explicit), env, tmp_path) == explicit


def test_env_var_used_when_no_cli(tmp_path):
    env_file = _touch(tmp_path / "env.yaml")
    _touch(tmp_path / "config.yaml")
    env = {"TRANSCRIBER_CONFIG": str(env_file)}
    assert main_mod.resolve_config_path(None, env, tmp_path) == env_file


@pytest.mark.parametrize("blank", ["", "   ", "\t"])
def test_empty_env_treated_as_unset(tmp_path, blank):
    cfg = _touch(tmp_path / "config.yaml")
    env = {"TRANSCRIBER_CONFIG": blank}
    # Falls back to CWD candidate rather than failing on an empty value.
    assert main_mod.resolve_config_path(None, env, tmp_path) == cfg


def test_transcriber_config_preferred_over_config_with_shadow_log(tmp_path, caplog):
    preferred = _touch(tmp_path / "transcriber.config.yaml")
    _touch(tmp_path / "config.yaml")
    with caplog.at_level(logging.INFO, logger="transcriber"):
        resolved = main_mod.resolve_config_path(None, {}, tmp_path)
    assert resolved == preferred
    assert any("shadow" in r.message.lower() for r in caplog.records)


def test_plain_config_yaml_used_when_only_one(tmp_path):
    cfg = _touch(tmp_path / "config.yaml")
    assert main_mod.resolve_config_path(None, {}, tmp_path) == cfg


def test_missing_explicit_config_raises_no_fallback(tmp_path):
    # A CWD candidate exists, but an explicit (missing) --config must NOT fall back.
    _touch(tmp_path / "config.yaml")
    with pytest.raises(ConfigNotFoundError):
        main_mod.resolve_config_path(str(tmp_path / "nope.yaml"), {}, tmp_path)


def test_missing_env_config_raises_no_fallback(tmp_path):
    _touch(tmp_path / "config.yaml")
    env = {"TRANSCRIBER_CONFIG": str(tmp_path / "nope.yaml")}
    with pytest.raises(ConfigNotFoundError):
        main_mod.resolve_config_path(None, env, tmp_path)


def test_directory_rejected(tmp_path):
    d = tmp_path / "adir"
    d.mkdir()
    with pytest.raises(ConfigNotFoundError):
        main_mod.resolve_config_path(str(d), {}, tmp_path)


def test_env_directory_rejected(tmp_path):
    # TRANSCRIBER_CONFIG pointing at a directory fails like --config does (L2).
    d = tmp_path / "adir"
    d.mkdir()
    env = {"TRANSCRIBER_CONFIG": str(d)}
    with pytest.raises(ConfigNotFoundError):
        main_mod.resolve_config_path(None, env, tmp_path)


def test_relative_cli_path_anchored_to_injected_cwd(tmp_path):
    # A relative --config is resolved against the injected cwd, not the process
    # CWD, and the returned path is anchored consistently (critique M1).
    _touch(tmp_path / "rel.yaml")
    resolved = main_mod.resolve_config_path("rel.yaml", {}, tmp_path)
    assert resolved == tmp_path / "rel.yaml"
    assert resolved.is_absolute() or resolved.parent == tmp_path


def test_relative_env_path_anchored_to_injected_cwd(tmp_path):
    _touch(tmp_path / "rel-env.yaml")
    env = {"TRANSCRIBER_CONFIG": "rel-env.yaml"}
    assert main_mod.resolve_config_path(None, env, tmp_path) == tmp_path / "rel-env.yaml"


def test_no_config_anywhere_raises_naming_sources(tmp_path):
    with pytest.raises(ConfigNotFoundError) as exc:
        main_mod.resolve_config_path(None, {}, tmp_path)
    msg = str(exc.value)
    assert "--config" in msg
    assert "TRANSCRIBER_CONFIG" in msg
    assert "transcriber.config.yaml" in msg
    assert "config.yaml" in msg


# --------------------------------------------------------------------------- #
# CLI argument-ordering regression (P5) + exit codes via main()
# --------------------------------------------------------------------------- #
def _write_min_config(path: Path, recordings_dir: Path) -> None:
    path.write_text(
        f"""recordings_dir: {recordings_dir.as_posix()}
stages:
  slides:
    enabled: false
    backend: openrouter
  s3_sync: false
transcribe:
  model_id: microsoft/mai-transcribe-2
summary:
  language: en
  sections: [overview]
  backend: agno
agent:
  output_file: "{{basename}}.md"
openrouter:
  api_key_env: OPENROUTER_API_KEY
notion:
  server: notion-x
  parent_page_id: pid
  insert: subpage
  token_env: NOTION_TOKEN
telegram:
  bot_token_env: TG_TOKEN
  default_chat_id: "123"
  routing: {{}}
""",
        encoding="utf-8",
    )


def test_config_flag_before_subcommand_not_clobbered(tmp_path, capsys, monkeypatch):
    """`transcriber --config X check` must use X, not fall back (P5)."""
    rec = tmp_path / "rec"
    rec.mkdir()
    cfg = tmp_path / "host.config.yaml"
    _write_min_config(cfg, rec)
    monkeypatch.setattr(main_mod.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setenv("OPENROUTER_API_KEY", "x")
    monkeypatch.setenv("NOTION_TOKEN", "x")
    monkeypatch.setenv("TG_TOKEN", "x")
    # Run from an empty CWD so any fallback would fail to find a config.
    monkeypatch.chdir(tmp_path / "rec")

    rc = main_mod.main(["--config", str(cfg), "check"])
    out = capsys.readouterr().out
    assert rc == 0
    assert str(cfg) in out  # path surfaced (R3)


def test_config_flag_after_subcommand(tmp_path, capsys, monkeypatch):
    rec = tmp_path / "rec"
    rec.mkdir()
    cfg = tmp_path / "host.config.yaml"
    _write_min_config(cfg, rec)
    monkeypatch.setattr(main_mod.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setenv("OPENROUTER_API_KEY", "x")
    monkeypatch.setenv("NOTION_TOKEN", "x")
    monkeypatch.setenv("TG_TOKEN", "x")
    rc = main_mod.main(["check", "--config", str(cfg)])
    assert rc == 0
    assert str(cfg) in capsys.readouterr().out


def test_missing_config_exit_2(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # empty dir, no candidates
    assert main_mod.main(["--dry-run"]) == 2


def test_dry_run_surfaces_and_logs_config_path(tmp_path, capsys, caplog, monkeypatch):
    rec = tmp_path / "rec"
    rec.mkdir()
    cfg = tmp_path / "config.yaml"
    _write_min_config(cfg, rec)
    monkeypatch.chdir(tmp_path)
    with caplog.at_level(logging.INFO, logger="transcriber"):
        rc = main_mod.main(["--dry-run"])
    out = capsys.readouterr().out
    assert rc == 0
    assert str(cfg) in out or "config.yaml" in out
    assert any("using config" in r.message for r in caplog.records)
