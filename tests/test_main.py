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
    OpenRouter,
    S3,
    Stages,
    SlidesStage,
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
    slides_backend: str = "openrouter",
    summary_backend: str = "agy",
    s3_sync: bool = False,
    s3_target: bool = False,
    openrouter: bool = False,
    notion_token_env: str | None = None,
) -> Config:
    s3 = S3(bucket="s3://bucket", profile="prof") if s3_target else None
    or_block = OpenRouter(api_key_env="OPENROUTER_API_KEY") if openrouter else None
    return Config(
        recordings_dir=str(recordings_dir),
        stages=Stages(
            slides=SlidesStage(enabled=slides, backend=slides_backend),
            s3_sync=s3_sync,
        ),
        transcribe=Transcribe(model_id="scribe_v1"),
        summary=Summary(language="en", sections=["overview"], backend=summary_backend),
        agent=Agent(cli="agy", extra_args=[], output_file="{basename}.md"),
        notion=Notion(
            server="notion-x",
            parent_page_id="pid",
            insert="subpage",
            token_env=notion_token_env,
        ),
        telegram=Telegram(bot_token_env="TG_TOKEN", default_chat_id="123", routing={}),
        timeouts=Timeouts(),
        s3=s3,
        openrouter=or_block,
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

    The summarize backend stub writes the summary ``.md`` (+ ``.telegram.md``)
    so downstream stages (telegram reading the digest, discovery treating the
    recording as done) work as in a real run. Backends are stubbed at the
    orchestrator seam (``_get_slides_backend`` / ``get_summarize_backend``) so
    no real agy / openrouter / agno internals are exercised — that keeps the
    test to Task 5's wiring surface.
    """
    calls: list[tuple[str, str]] = []
    # Records (transcript_stem, slides_markdown) seen by the summarize backend.
    summarize_seen: list[tuple[str, object]] = []
    # Records (backend_name, slide_count) for describe_slides backend runs.
    slides_seen: list[tuple[str, int]] = []

    def fake_pipeline(mp4, config):
        calls.append(("pipeline", Path(mp4).stem))
        # The real pipeline writes the <name>.txt transcript; the describe_slides
        # step reads it, so materialize it here.
        (Path(mp4).parent / f"{Path(mp4).stem}.txt").write_text(
            "transcript body", encoding="utf-8"
        )
        return pipeline_mod.RecordingResult(name=Path(mp4).stem, mp4=Path(mp4))

    from transcriber.backends.interfaces import SummaryResult

    class FakeSlidesBackend:
        def __init__(self, backend_name):
            self._backend_name = backend_name

        def describe(self, slides, transcript_text, config, *, timeout):
            slides_seen.append((self._backend_name, len(list(slides))))
            calls.append(("describe_slides", self._backend_name))
            return "slide markdown"

    class FakeSummarizeBackend:
        def __init__(self, backend_name):
            self._backend_name = backend_name

        def summarize(self, transcript_path, slides_markdown, recording_dir, config):
            name = Path(transcript_path).stem
            calls.append(("agent", name))
            summarize_seen.append((name, slides_markdown))
            md = Path(recording_dir) / f"{name}.md"
            md.write_text("summary body", encoding="utf-8")
            (Path(recording_dir) / f"{name}.telegram.md").write_text(
                "digest body", encoding="utf-8"
            )
            return SummaryResult(
                summary_path=md,
                telegram_path=Path(recording_dir) / f"{name}.telegram.md",
            )

    def fake_get_slides_backend(config):
        return FakeSlidesBackend(config.stages.slides.backend)

    def fake_get_summarize_backend(config):
        return FakeSummarizeBackend(config.summary.backend)

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
    monkeypatch.setattr(main_mod, "_get_slides_backend", fake_get_slides_backend)
    monkeypatch.setattr(
        main_mod.backends_mod, "get_summarize_backend", fake_get_summarize_backend
    )
    monkeypatch.setattr(main_mod, "TelegramPublisher", FakeTelegram)
    monkeypatch.setattr(main_mod.s3_mod, "sync", fake_sync)
    monkeypatch.setattr(main_mod.cleanup_mod, "cleanup", fake_cleanup)

    return {
        "calls": calls,
        "sent": sent,
        "summarize_seen": summarize_seen,
        "slides_seen": slides_seen,
    }


# --------------------------------------------------------------------------- #
# Pre-flight check (E2)
# --------------------------------------------------------------------------- #
def test_preflight_fails_on_missing_binary(monkeypatch, tmp_path):
    config = make_config(tmp_path, slides=False, s3_sync=False)
    monkeypatch.setenv("TG_TOKEN", "abc")
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-key")
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
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-key")
    assert main_mod.main(["check", "--config", str(config_path)]) == 0


def test_preflight_requires_aws_only_when_s3_enabled(monkeypatch, tmp_path):
    config = make_config(tmp_path, slides=False, s3_sync=True, s3_target=True)
    monkeypatch.setenv("TG_TOKEN", "abc")
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-key")

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
  slides:
    enabled: true
    backend: openrouter
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
openrouter:
  api_key_env: OPENROUTER_API_KEY
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
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-key")

    rc = main_mod.main(["--config", str(config_path)])
    assert rc == 0

    calls = stage_calls["calls"]
    # Per-recording order (s3 disabled so no s3 stage).
    assert calls == [
        ("pipeline", "a"),
        ("describe_slides", "openrouter"),
        ("agent", "a"),
        ("telegram", "-"),
        ("cleanup", "a"),
        ("pipeline", "b"),
        ("describe_slides", "openrouter"),
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
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-key")

    rc = main_mod.main(["--config", str(config_path)])
    assert rc == 0
    calls = stage_calls["calls"]
    assert calls == [
        ("pipeline", "a"),
        ("describe_slides", "openrouter"),
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
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-key")

    # Make the summarize backend fail for recording "a" only.
    orig_calls = stage_calls["calls"]
    from transcriber.backends.interfaces import SummaryResult

    class FailingSummarizeBackend:
        def __init__(self, backend_name):
            self._backend_name = backend_name

        def summarize(self, transcript_path, slides_markdown, recording_dir, config):
            name = Path(transcript_path).stem
            orig_calls.append(("agent", name))
            if name == "a":
                raise RuntimeError("agy blew up")
            md = Path(recording_dir) / f"{name}.md"
            md.write_text("summary", encoding="utf-8")
            (Path(recording_dir) / f"{name}.telegram.md").write_text(
                "digest", encoding="utf-8"
            )
            return SummaryResult(
                summary_path=md,
                telegram_path=Path(recording_dir) / f"{name}.telegram.md",
            )

    monkeypatch.setattr(
        main_mod.backends_mod,
        "get_summarize_backend",
        lambda config: FailingSummarizeBackend(config.summary.backend),
    )

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
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-key")

    # Pre-mark pipeline + describe_slides + summarize + notion + telegram complete.
    st = state_mod.RecordingState(mp4)
    for stage in ("pipeline", "describe_slides", "summarize", "notion", "telegram"):
        st.mark_complete(stage)
    # Provide the summary so discovery still sees it as "new" only if md absent.
    # Discovery uses md existence; keep md absent so the recording is processed.

    rc = main_mod.main(["--config", str(config_path)])
    assert rc == 0

    calls = stage_calls["calls"]
    # pipeline/slides/agent/telegram were already complete -> skipped. Only cleanup runs.
    assert ("pipeline", "a") not in calls
    assert ("describe_slides", "agy") not in calls
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
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-key")

    main_mod.main(["--config", str(config_path)])
    calls = stage_calls["calls"]
    assert ("s3", "-") not in calls


def test_disabled_slides_ignores_stale_slides_md(stage_calls, monkeypatch, tmp_path):
    """Regression (Copilot #4): with slides disabled, a stale ``<name>.slides.md``
    must NOT be fed into the summary — the summarize backend receives ``None``.
    """
    recordings_dir = tmp_path / "rec"
    mp4 = make_recording(recordings_dir, "a")

    # A stale slide-description artifact left over from a prior slides-on run.
    (recordings_dir / "a.slides.md").write_text(
        "### STALE SLIDE DESCRIPTIONS\n", encoding="utf-8"
    )

    # A slides-DISABLED config file.
    config_path = tmp_path / "c.yaml"
    config_path.write_text(
        f"""recordings_dir: {recordings_dir.as_posix()}
stages:
  slides:
    enabled: false
    backend: openrouter
  s3_sync: false
transcribe:
  model_id: scribe_v1
summary:
  language: en
  sections: [overview]
  backend: agy
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
""",
        encoding="utf-8",
    )

    monkeypatch.setattr(main_mod.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setenv("TG_TOKEN", "abc")
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-key")

    rc = main_mod.main(["--config", str(config_path)])
    assert rc == 0

    # describe_slides did not run (disabled) ...
    assert not any(c[0] == "describe_slides" for c in stage_calls["calls"])
    # ... and the summarize backend saw slides_markdown=None, NOT the stale file.
    summarize_seen = stage_calls["summarize_seen"]
    assert summarize_seen, "summarize backend should have run"
    _name, slides_markdown = summarize_seen[0]
    assert slides_markdown is None


def test_disabled_slides_use_txt_and_no_slides(monkeypatch, tmp_path):
    recordings_dir = tmp_path / "rec"
    mp4 = make_recording(recordings_dir, "a")
    config = make_config(recordings_dir, slides=False, s3_sync=False)

    # transcript path is always the .txt transcript.
    assert main_mod._transcript_path(mp4, config).name == "a.txt"
    # slides disabled -> empty slide list even if a slides dir exists.
    (recordings_dir / "extracted_slides.a").mkdir()
    assert main_mod._slide_image_paths(mp4, config) == []

    # enabled-stage plan omits scene-extract + describe-slides + s3.
    stages = main_mod._enabled_stage_names(config)
    assert "scene-extract" not in stages
    assert not any(s.startswith("describe-slides") for s in stages)
    assert "parse-transcript" not in stages
    assert "s3-sync" not in stages
    # summarize token still present with the (default) backend surfaced.
    assert "summarize+notion [agy]" in stages


def test_enabled_toggles_include_stages(tmp_path):
    config = make_config(
        tmp_path, slides=True, s3_sync=True, s3_target=True
    )
    stages = main_mod._enabled_stage_names(config)
    assert "scene-extract" in stages
    assert "describe-slides [openrouter]" in stages
    assert "summarize+notion [agy]" in stages
    assert "transcribe" in stages
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



# --------------------------------------------------------------------------- #
# Task 5: pinned dry-run tokens
# --------------------------------------------------------------------------- #
def test_dry_run_tokens_pinned():
    """scene-extract (not slides), describe-slides [<backend>], summarize+notion [<backend>]."""
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        base = Path(d)
        config = make_config(
            base,
            slides=True,
            slides_backend="openrouter",
            summary_backend="agno",
            openrouter=True,
            notion_token_env="NOTION_TOKEN",
        )
        stages = main_mod._enabled_stage_names(config)
        # Renamed pipeline slide-extraction token.
        assert "scene-extract" in stages
        assert "slides" not in stages
        # Post-transcript backend tokens surface the selected backend.
        assert "describe-slides [openrouter]" in stages
        assert "summarize+notion [agno]" in stages
        # Ordering: describe-slides comes after transcribe and before summarize.
        assert stages.index("transcribe") < stages.index("describe-slides [openrouter]")
        assert stages.index("describe-slides [openrouter]") < stages.index(
            "summarize+notion [agno]"
        )


def test_describe_slides_token_only_when_slides_on(tmp_path):
    on = main_mod._enabled_stage_names(make_config(tmp_path, slides=True))
    off = main_mod._enabled_stage_names(make_config(tmp_path, slides=False))
    assert any(s.startswith("describe-slides") for s in on)
    assert not any(s.startswith("describe-slides") for s in off)
    assert "scene-extract" in on
    assert "scene-extract" not in off


# --------------------------------------------------------------------------- #
# Task 5: describe_slides backend selection + gating
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("slides_backend", ["agy", "openrouter"])
def test_describe_slides_runs_configured_backend(
    stage_calls, monkeypatch, tmp_path, slides_backend
):
    recordings_dir = tmp_path / "rec"
    make_recording(recordings_dir, "a")
    config = make_config(
        recordings_dir,
        slides=True,
        slides_backend=slides_backend,
        openrouter=(slides_backend == "openrouter"),
    )
    monkeypatch.setattr(main_mod.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setenv("TG_TOKEN", "abc")
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-key")
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-key")

    outcomes = main_mod.run_batch(config, main_mod.discover_new_recordings(config))
    assert all(o.succeeded for o in outcomes)

    # The configured backend was the one invoked.
    assert stage_calls["slides_seen"] == [(slides_backend, 0)]
    assert ("describe_slides", slides_backend) in stage_calls["calls"]
    # <name>.slides.md written.
    assert (recordings_dir / "a.slides.md").read_text(encoding="utf-8") == "slide markdown"


def test_describe_slides_skipped_when_disabled(stage_calls, monkeypatch, tmp_path):
    recordings_dir = tmp_path / "rec"
    make_recording(recordings_dir, "a")
    config = make_config(recordings_dir, slides=False)
    monkeypatch.setattr(main_mod.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setenv("TG_TOKEN", "abc")
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-key")

    main_mod.run_batch(config, main_mod.discover_new_recordings(config))
    assert stage_calls["slides_seen"] == []
    assert not any(c[0] == "describe_slides" for c in stage_calls["calls"])
    # No slides.md written.
    assert not (recordings_dir / "a.slides.md").exists()


def test_describe_slides_manifest_gated_skips_paid_call_on_rerun(
    stage_calls, monkeypatch, tmp_path
):
    recordings_dir = tmp_path / "rec"
    mp4 = make_recording(recordings_dir, "a")
    config = make_config(recordings_dir, slides=True)
    monkeypatch.setattr(main_mod.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setenv("TG_TOKEN", "abc")
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-key")

    # Pre-mark pipeline + describe_slides complete and drop the artifact +
    # transcript that a prior run would have produced.
    st = state_mod.RecordingState(mp4)
    st.mark_complete("pipeline")
    st.mark_complete("describe_slides")
    (recordings_dir / "a.txt").write_text("t", encoding="utf-8")
    (recordings_dir / "a.slides.md").write_text("prior", encoding="utf-8")

    main_mod.run_batch(config, main_mod.discover_new_recordings(config))
    # No slides backend re-run.
    assert stage_calls["slides_seen"] == []
    assert not any(c[0] == "describe_slides" for c in stage_calls["calls"])


def test_describe_slides_idempotent_skip_when_artifact_present(
    stage_calls, monkeypatch, tmp_path
):
    """Artifact present but manifest lacks describe_slides -> skip backend, mark complete."""
    recordings_dir = tmp_path / "rec"
    mp4 = make_recording(recordings_dir, "a")
    config = make_config(recordings_dir, slides=True)
    monkeypatch.setattr(main_mod.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setenv("TG_TOKEN", "abc")
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-key")

    st = state_mod.RecordingState(mp4)
    st.mark_complete("pipeline")  # no describe_slides mark
    (recordings_dir / "a.txt").write_text("t", encoding="utf-8")
    (recordings_dir / "a.slides.md").write_text("already", encoding="utf-8")

    main_mod.run_batch(config, main_mod.discover_new_recordings(config))
    # Backend NOT called (artifact present), but stage marked complete.
    assert stage_calls["slides_seen"] == []
    st2 = state_mod.RecordingState(mp4)
    assert st2.is_complete("describe_slides")


# --------------------------------------------------------------------------- #
# Task 5: summarize receives slide markdown (no image paths)
# --------------------------------------------------------------------------- #
def test_summarize_receives_slide_markdown(stage_calls, monkeypatch, tmp_path):
    recordings_dir = tmp_path / "rec"
    make_recording(recordings_dir, "a")
    config = make_config(recordings_dir, slides=True)
    monkeypatch.setattr(main_mod.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setenv("TG_TOKEN", "abc")
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-key")

    main_mod.run_batch(config, main_mod.discover_new_recordings(config))

    assert stage_calls["summarize_seen"] == [("a", "slide markdown")]
    seen_md = stage_calls["summarize_seen"][0][1]
    # Slide markdown is text, never image paths.
    assert ".jpg" not in seen_md and ".png" not in seen_md
    assert "extracted_slides" not in seen_md


def test_summarize_receives_none_when_slides_disabled(stage_calls, monkeypatch, tmp_path):
    recordings_dir = tmp_path / "rec"
    make_recording(recordings_dir, "a")
    config = make_config(recordings_dir, slides=False)
    monkeypatch.setattr(main_mod.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setenv("TG_TOKEN", "abc")
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-key")

    main_mod.run_batch(config, main_mod.discover_new_recordings(config))
    # No slides.md -> summarize gets None.
    assert stage_calls["summarize_seen"] == [("a", None)]


# --------------------------------------------------------------------------- #
# Task 5: pre-flight backend-aware requirements
# --------------------------------------------------------------------------- #
def test_preflight_openrouter_required_iff_slides_openrouter(monkeypatch, tmp_path):
    monkeypatch.setattr(main_mod.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setenv("TG_TOKEN", "abc")
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-key")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    # slides=openrouter -> OpenRouter key required.
    need = make_config(
        tmp_path, slides=True, slides_backend="openrouter", openrouter=True
    )
    assert any("OPENROUTER_API_KEY" in p for p in main_mod.preflight_check(need))

    # slides=agy, summary=agy -> OpenRouter not required.
    no_need = make_config(tmp_path, slides=True, slides_backend="agy")
    assert not any("OPENROUTER_API_KEY" in p for p in main_mod.preflight_check(no_need))


def test_preflight_openrouter_and_notion_required_iff_summary_agno(monkeypatch, tmp_path):
    monkeypatch.setattr(main_mod.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setenv("TG_TOKEN", "abc")
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-key")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("NOTION_TOKEN", raising=False)

    agno = make_config(
        tmp_path,
        slides=False,
        summary_backend="agno",
        openrouter=True,
        notion_token_env="NOTION_TOKEN",
    )
    problems = main_mod.preflight_check(agno)
    assert any("OPENROUTER_API_KEY" in p for p in problems)
    assert any("NOTION_TOKEN" in p for p in problems)

    # summary=agy -> neither required.
    agy = make_config(tmp_path, slides=False, summary_backend="agy")
    problems2 = main_mod.preflight_check(agy)
    assert not any("OPENROUTER_API_KEY" in p for p in problems2)
    assert not any("NOTION_TOKEN" in p for p in problems2)


def test_preflight_agy_required_iff_used(monkeypatch, tmp_path):
    monkeypatch.setenv("TG_TOKEN", "abc")
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-key")
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-key")
    monkeypatch.setenv("NOTION_TOKEN", "n-key")

    def which_no_agy(name):
        return None if name == "agy" else f"/usr/bin/{name}"

    monkeypatch.setattr(main_mod.shutil, "which", which_no_agy)

    # No agy anywhere (slides=openrouter, summary=agno) -> agy NOT required.
    no_agy = make_config(
        tmp_path,
        slides=True,
        slides_backend="openrouter",
        summary_backend="agno",
        openrouter=True,
        notion_token_env="NOTION_TOKEN",
    )
    assert not any("agy" in p for p in main_mod.preflight_check(no_agy))

    # summary=agy uses agy -> required (and missing).
    uses_agy = make_config(
        tmp_path, slides=True, slides_backend="openrouter", summary_backend="agy",
        openrouter=True,
    )
    assert any("agy" in p for p in main_mod.preflight_check(uses_agy))


# --------------------------------------------------------------------------- #
# Task 5: shared openrouter block, distinct models per stage
# --------------------------------------------------------------------------- #
def test_shared_config_distinct_models(tmp_path):
    """slides uses slides_model; agno summary uses summary_model from one block."""
    config = make_config(
        tmp_path,
        slides=True,
        slides_backend="openrouter",
        summary_backend="agno",
        openrouter=True,
        notion_token_env="NOTION_TOKEN",
    )
    assert config.openrouter is not None
    # One openrouter block, two distinct model fields.
    assert config.openrouter.slides_model != config.openrouter.summary_model
    # Backend selection is a pure function of config.
    assert isinstance(
        main_mod._get_slides_backend(config),
        main_mod.backends_mod.OpenRouterSlidesBackend,
    )


def test_get_slides_backend_selection(tmp_path):
    orr = make_config(
        tmp_path, slides=True, slides_backend="openrouter", openrouter=True
    )
    assert isinstance(
        main_mod._get_slides_backend(orr),
        main_mod.backends_mod.OpenRouterSlidesBackend,
    )
    # agy is no longer a valid slides backend; selection rejects it.
    agy = make_config(tmp_path, slides=True, slides_backend="agy")
    with pytest.raises(ValueError):
        main_mod._get_slides_backend(agy)
