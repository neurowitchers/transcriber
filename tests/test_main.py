"""Tests for the orchestrator + CLI (``transcriber.__main__``).

All externals are mocked: the pipeline / agent / telegram / s3 / cleanup stage
functions are monkeypatched so no real binaries, network, or agy are invoked.

Coverage:
* pre-flight failure on a missing binary and on a missing env var;
* ``--dry-run`` prints a plan and performs NO side effects;
* happy path over 2 recordings (all stages called, in order);
* failure isolation (one recording fails -> its cleanup skipped, the other
  still processes fully);
* the state manifest causes an already-complete stage to be skipped on re-run;
* disabled toggles (slides / parse / s3) skip their stages.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from transcriber import __main__ as main_mod
from transcriber import agent as agent_mod
from transcriber import cleanup as cleanup_mod
from transcriber import pipeline as pipeline_mod
from transcriber import state as state_mod
from transcriber.config import (
    Agent,
    Config,
    Notion,
    S3,
    Stages,
    Summary,
    Telegram,
    Timeouts,
    Transcribe,
)
from transcriber.publish import s3 as s3_mod
from transcriber.publish import telegram as telegram_mod


# --------------------------------------------------------------------------- #
# Config + fixture helpers
# --------------------------------------------------------------------------- #
def make_config(
    recordings_dir: Path,
    *,
    slides: bool = True,
    parse_transcript: bool = True,
    s3_sync: bool = False,
    s3_target: bool = False,
) -> Config:
    s3 = S3(bucket="s3://bucket", profile="prof") if s3_target else None
    return Config(
        recordings_dir=str(recordings_dir),
        stages=Stages(slides=slides, parse_transcript=parse_transcript, s3_sync=s3_sync),
        transcribe=Transcribe(model_id="scribe_v1"),
        summary=Summary(language="en", sections=["overview"]),
        agent=Agent(cli="agy", extra_args=[], output_file="{basename}.md"),
        notion=Notion(server="notion-x", parent_page_id="pid", insert="subpage"),
        telegram=Telegram(bot_token_env="TG_TOKEN", default_chat_id="123", routing={}),
        timeouts=Timeouts(),
        s3=s3,
    )


def make_recording(recordings_dir: Path, name: str) -> Path:
    """Create a bare ``.mp4`` (no ``.md`` yet, so it counts as new)."""
    recordings_dir.mkdir(parents=True, exist_ok=True)
    mp4 = recordings_dir / f"{name}.mp4"
    mp4.write_text("video", encoding="utf-8")
    return mp4


@pytest.fixture
def stage_calls(monkeypatch):
    """Monkeypatch all stage functions; record calls in order.

    The agent stub also writes the summary ``.md`` so downstream stages (telegram
    reading the summary, discovery treating the recording as done) work as in a
    real run.
    """
    calls: list[tuple[str, str]] = []

    def fake_pipeline(mp4, config):
        calls.append(("pipeline", Path(mp4).stem))
        return pipeline_mod.RecordingResult(name=Path(mp4).stem, mp4=Path(mp4))

    def fake_run_agent(config, transcript_path, recording_dir, slide_image_paths=None):
        name = Path(transcript_path).stem
        calls.append(("agent", name))
        md = Path(recording_dir) / f"{name}.md"
        md.write_text("summary body", encoding="utf-8")
        # Also write the concise Telegram digest, as the real agent does.
        (Path(recording_dir) / f"{name}.telegram.md").write_text(
            "digest body", encoding="utf-8"
        )
        return md

    sent: list[tuple[str, str]] = []

    class FakeTelegram:
        def __init__(self, config, client=None):
            self._config = config

        def send(self, text, topic=None):
            calls.append(("telegram", "-"))
            sent.append((text, topic))
            return []

    def fake_sync(local_dir, config):
        calls.append(("s3", "-"))
        return True

    def fake_cleanup(recording, keep_intermediates=False):
        calls.append(("cleanup", Path(recording).stem))
        return []

    monkeypatch.setattr(main_mod.pipeline_mod, "process_recording", fake_pipeline)
    monkeypatch.setattr(main_mod.agent_mod, "run_agent", fake_run_agent)
    monkeypatch.setattr(main_mod, "TelegramPublisher", FakeTelegram)
    monkeypatch.setattr(main_mod.s3_mod, "sync", fake_sync)
    monkeypatch.setattr(main_mod.cleanup_mod, "cleanup", fake_cleanup)

    return {"calls": calls, "sent": sent}


# --------------------------------------------------------------------------- #
# Pre-flight check (E2)
# --------------------------------------------------------------------------- #
def test_preflight_fails_on_missing_binary(monkeypatch, tmp_path):
    config = make_config(tmp_path, slides=False, s3_sync=False)
    monkeypatch.setenv("TG_TOKEN", "abc")
    # All binaries missing.
    monkeypatch.setattr(main_mod.shutil, "which", lambda name: None)

    problems = main_mod.preflight_check(config)
    assert any("ffmpeg" in p for p in problems)
    assert any("binary" in p for p in problems)


def test_preflight_fails_on_missing_env_var(monkeypatch, tmp_path):
    config = make_config(tmp_path, slides=False, s3_sync=False)
    monkeypatch.delenv("TG_TOKEN", raising=False)
    # All binaries present.
    monkeypatch.setattr(main_mod.shutil, "which", lambda name: f"/usr/bin/{name}")

    problems = main_mod.preflight_check(config)
    assert any("TG_TOKEN" in p and "environment variable" in p for p in problems)


def test_check_subcommand_exit_codes(monkeypatch, tmp_path):
    config_path = tmp_path / "c.yaml"
    _write_yaml_config(config_path, tmp_path, s3_sync=False)

    # Failing: no binaries.
    monkeypatch.setattr(main_mod.shutil, "which", lambda name: None)
    monkeypatch.delenv("TG_TOKEN", raising=False)
    assert main_mod.main(["check", "--config", str(config_path)]) == 1

    # Passing: all present.
    monkeypatch.setattr(main_mod.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setenv("TG_TOKEN", "abc")
    assert main_mod.main(["check", "--config", str(config_path)]) == 0


def test_preflight_requires_aws_only_when_s3_enabled(monkeypatch, tmp_path):
    config = make_config(tmp_path, slides=False, s3_sync=True, s3_target=True)
    monkeypatch.setenv("TG_TOKEN", "abc")

    def which(name):
        return None if name == "aws" else f"/usr/bin/{name}"

    monkeypatch.setattr(main_mod.shutil, "which", which)
    problems = main_mod.preflight_check(config)
    assert any("aws" in p for p in problems)

    # With s3 disabled, aws is not required.
    config2 = make_config(tmp_path, slides=False, s3_sync=False)
    problems2 = main_mod.preflight_check(config2)
    assert not any("aws" in p for p in problems2)


# --------------------------------------------------------------------------- #
# YAML config writer (drives main() end-to-end with a real config file)
# --------------------------------------------------------------------------- #
def _write_yaml_config(path: Path, recordings_dir: Path, *, s3_sync: bool) -> None:
    s3_block = ""
    if s3_sync:
        s3_block = "s3:\n  bucket: s3://bucket\n  profile: prof\n"
    path.write_text(
        f"""recordings_dir: {recordings_dir.as_posix()}
stages:
  slides: true
  parse_transcript: true
  s3_sync: {str(s3_sync).lower()}
transcribe:
  model_id: scribe_v1
summary:
  language: en
  sections: [overview]
agent:
  cli: agy
  extra_args: []
  output_file: "{{basename}}.md"
notion:
  server: notion-x
  parent_page_id: pid
  insert: subpage
telegram:
  bot_token_env: TG_TOKEN
  default_chat_id: "123"
  routing: {{}}
{s3_block}""",
        encoding="utf-8",
    )


# --------------------------------------------------------------------------- #
# --dry-run (P1): plan printed, NO side effects
# --------------------------------------------------------------------------- #
def test_dry_run_prints_plan_and_no_side_effects(stage_calls, tmp_path, capsys):
    recordings_dir = tmp_path / "rec"
    make_recording(recordings_dir, "meeting-a")
    make_recording(recordings_dir, "meeting-b")
    config_path = tmp_path / "c.yaml"
    _write_yaml_config(config_path, recordings_dir, s3_sync=False)

    rc = main_mod.main(["--config", str(config_path), "--dry-run"])
    out = capsys.readouterr().out

    assert rc == 0
    assert "Execution plan" in out
    assert "meeting-a.mp4" in out
    assert "meeting-b.mp4" in out
    assert "Notion" in out
    assert "Telegram" in out
    # NO stage functions invoked.
    assert stage_calls["calls"] == []
    # No manifests written.
    assert list(recordings_dir.glob("*.transcriber_state.json")) == []


def test_dry_run_no_new_recordings(stage_calls, tmp_path, capsys):
    recordings_dir = tmp_path / "rec"
    recordings_dir.mkdir()
    config_path = tmp_path / "c.yaml"
    _write_yaml_config(config_path, recordings_dir, s3_sync=False)

    rc = main_mod.main(["--config", str(config_path), "--dry-run"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "no new recordings" in out
    assert stage_calls["calls"] == []


# --------------------------------------------------------------------------- #
# Happy path over 2 recordings: all stages in order
# --------------------------------------------------------------------------- #
def test_happy_path_two_recordings(stage_calls, monkeypatch, tmp_path):
    recordings_dir = tmp_path / "rec"
    make_recording(recordings_dir, "a")
    make_recording(recordings_dir, "b")
    config_path = tmp_path / "c.yaml"
    _write_yaml_config(config_path, recordings_dir, s3_sync=False)

    monkeypatch.setattr(main_mod.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setenv("TG_TOKEN", "abc")

    rc = main_mod.main(["--config", str(config_path)])
    assert rc == 0

    calls = stage_calls["calls"]
    # Per-recording order (s3 disabled so no s3 stage).
    assert calls == [
        ("pipeline", "a"),
        ("agent", "a"),
        ("telegram", "-"),
        ("cleanup", "a"),
        ("pipeline", "b"),
        ("agent", "b"),
        ("telegram", "-"),
        ("cleanup", "b"),
    ]


def test_happy_path_with_s3(stage_calls, monkeypatch, tmp_path):
    recordings_dir = tmp_path / "rec"
    make_recording(recordings_dir, "a")
    config_path = tmp_path / "c.yaml"
    _write_yaml_config(config_path, recordings_dir, s3_sync=True)

    monkeypatch.setattr(main_mod.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setenv("TG_TOKEN", "abc")

    rc = main_mod.main(["--config", str(config_path)])
    assert rc == 0
    calls = stage_calls["calls"]
    assert calls == [
        ("pipeline", "a"),
        ("agent", "a"),
        ("telegram", "-"),
        ("s3", "-"),
        ("cleanup", "a"),
    ]


# --------------------------------------------------------------------------- #
# Failure isolation
# --------------------------------------------------------------------------- #
def test_failure_isolation_skips_cleanup_but_continues(stage_calls, monkeypatch, tmp_path):
    recordings_dir = tmp_path / "rec"
    make_recording(recordings_dir, "a")
    make_recording(recordings_dir, "b")
    config_path = tmp_path / "c.yaml"
    _write_yaml_config(config_path, recordings_dir, s3_sync=False)

    monkeypatch.setattr(main_mod.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setenv("TG_TOKEN", "abc")

    # Make the agent fail for recording "a" only.
    orig_calls = stage_calls["calls"]

    def failing_agent(config, transcript_path, recording_dir, slide_image_paths=None):
        name = Path(transcript_path).stem
        orig_calls.append(("agent", name))
        if name == "a":
            raise RuntimeError("agy blew up")
        md = Path(recording_dir) / f"{name}.md"
        md.write_text("summary", encoding="utf-8")
        return md

    monkeypatch.setattr(main_mod.agent_mod, "run_agent", failing_agent)

    rc = main_mod.main(["--config", str(config_path)])
    # A failed -> overall non-zero.
    assert rc == 1

    calls = stage_calls["calls"]
    # "a" fails at agent: no telegram/cleanup for a. "b" processes fully.
    assert ("cleanup", "a") not in calls
    assert ("telegram", "-") in calls  # b's telegram
    assert ("pipeline", "b") in calls
    assert ("cleanup", "b") in calls

    # a's manifest records pipeline complete but NOT summarize/cleanup.
    st_a = state_mod.RecordingState(recordings_dir / "a.mp4")
    assert st_a.is_complete("pipeline")
    assert not st_a.is_complete("summarize")
    assert not st_a.is_complete("cleanup")


# --------------------------------------------------------------------------- #
# State manifest: completed stage skipped on re-run
# --------------------------------------------------------------------------- #
def test_manifest_skips_completed_stages_on_rerun(stage_calls, monkeypatch, tmp_path):
    recordings_dir = tmp_path / "rec"
    mp4 = make_recording(recordings_dir, "a")
    config_path = tmp_path / "c.yaml"
    _write_yaml_config(config_path, recordings_dir, s3_sync=False)

    monkeypatch.setattr(main_mod.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setenv("TG_TOKEN", "abc")

    # Pre-mark pipeline + summarize + notion + telegram complete.
    st = state_mod.RecordingState(mp4)
    for stage in ("pipeline", "summarize", "notion", "telegram"):
        st.mark_complete(stage)
    # Provide the summary so discovery still sees it as "new" only if md absent.
    # Discovery uses md existence; keep md absent so the recording is processed.

    rc = main_mod.main(["--config", str(config_path)])
    assert rc == 0

    calls = stage_calls["calls"]
    # pipeline/agent/telegram were already complete -> skipped. Only cleanup runs.
    assert ("pipeline", "a") not in calls
    assert ("agent", "a") not in calls
    assert ("telegram", "-") not in calls
    assert ("cleanup", "a") in calls


# --------------------------------------------------------------------------- #
# Disabled toggles skip stages
# --------------------------------------------------------------------------- #
def test_disabled_s3_toggle_skips_s3(stage_calls, monkeypatch, tmp_path):
    recordings_dir = tmp_path / "rec"
    make_recording(recordings_dir, "a")
    config_path = tmp_path / "c.yaml"
    _write_yaml_config(config_path, recordings_dir, s3_sync=False)

    monkeypatch.setattr(main_mod.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setenv("TG_TOKEN", "abc")

    main_mod.main(["--config", str(config_path)])
    calls = stage_calls["calls"]
    assert ("s3", "-") not in calls


def test_disabled_slides_and_parse_use_jsonl_and_no_slides(monkeypatch, tmp_path):
    recordings_dir = tmp_path / "rec"
    mp4 = make_recording(recordings_dir, "a")
    config = make_config(
        recordings_dir, slides=False, parse_transcript=False, s3_sync=False
    )

    # transcript path -> jsonl when parse disabled.
    assert main_mod._transcript_path(mp4, config).name == "a.jsonl"
    # slides disabled -> empty slide list even if a slides dir exists.
    (recordings_dir / "extracted_slides.a").mkdir()
    assert main_mod._slide_image_paths(mp4, config) == []

    # enabled-stage plan omits slides + parse + s3.
    stages = main_mod._enabled_stage_names(config)
    assert "slides" not in stages
    assert "parse-transcript" not in stages
    assert "s3-sync" not in stages


def test_enabled_toggles_include_stages(tmp_path):
    config = make_config(
        tmp_path, slides=True, parse_transcript=True, s3_sync=True, s3_target=True
    )
    stages = main_mod._enabled_stage_names(config)
    assert "slides" in stages
    assert "parse-transcript" in stages
    assert "s3-sync" in stages


# --------------------------------------------------------------------------- #
# Discovery: a recording with an existing .md is not "new"
# --------------------------------------------------------------------------- #
def test_discovery_excludes_recordings_with_existing_summary(tmp_path):
    recordings_dir = tmp_path / "rec"
    make_recording(recordings_dir, "a")
    make_recording(recordings_dir, "b")
    # b already has a summary.
    (recordings_dir / "b.md").write_text("done", encoding="utf-8")
    config = make_config(recordings_dir)

    new = main_mod.discover_new_recordings(config)
    names = [p.stem for p in new]
    assert names == ["a"]
