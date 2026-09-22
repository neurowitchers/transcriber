"""Tests for transcriber.publish.s3.

``duct`` is mocked: we intercept ``duct.cmd`` to capture the argument vector and
return a fake handle whose ``poll()`` reports immediate completion. No real
``aws`` process is ever spawned.
"""

from __future__ import annotations

import pytest

from transcriber import config as config_mod
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


def _make_config(*, s3_sync: bool, with_s3: bool, s3_timeout: int = 500) -> Config:
    return Config(
        recordings_dir="./recordings",
        stages=Stages(slides=True, parse_transcript=True, s3_sync=s3_sync),
        transcribe=Transcribe(model_id="scribe_v1"),
        summary=Summary(language="en", sections=["overview"]),
        agent=Agent(cli="kiro", extra_args=[], output_file="{basename}.md"),
        notion=Notion(server="n", parent_page_id="p", insert="subpage"),
        telegram=Telegram(bot_token_env="TOK", default_chat_id="1", routing={}),
        timeouts=Timeouts(s3=s3_timeout),
        s3=S3(bucket="s3://my-bucket/recordings/", profile="acme")
        if with_s3
        else None,
    )


class _FakeOutput:
    def __init__(self, status=0):
        self.status = status


class _FakeHandle:
    """Records lifecycle calls; poll() completes immediately by default."""

    def __init__(self, status=0, complete_immediately=True):
        self._status = status
        self._complete_immediately = complete_immediately
        self.polled = 0
        self.killed = False
        self.waited = False

    def poll(self):
        self.polled += 1
        if self._complete_immediately:
            return _FakeOutput(self._status)
        return None  # never completes -> forces timeout

    def kill(self):
        self.killed = True

    def wait(self):
        self.waited = True
        return _FakeOutput(self._status)


class _CmdRecorder:
    """Callable stand-in for ``duct.cmd`` capturing args and returning a handle."""

    def __init__(self, handle):
        self.handle = handle
        self.calls: list[tuple] = []

    def __call__(self, *args):
        self.calls.append(args)
        return self  # so that .start() can be chained

    def start(self):
        return self.handle


# --------------------------------------------------------------------------- #
# Enabled: correct command line
# --------------------------------------------------------------------------- #
def test_sync_builds_correct_command_line(monkeypatch):
    cfg = _make_config(s3_sync=True, with_s3=True)
    handle = _FakeHandle(status=0)
    recorder = _CmdRecorder(handle)
    monkeypatch.setattr(s3_mod.duct, "cmd", recorder)

    result = s3_mod.sync("/local/rec-dir", cfg)

    assert result is True
    assert recorder.calls == [
        (
            "aws",
            "s3",
            "sync",
            "/local/rec-dir",
            "s3://my-bucket/recordings/",
            "--profile",
            "acme",
        )
    ]


def test_build_sync_command_shape():
    cfg = _make_config(s3_sync=True, with_s3=True)
    args = s3_mod.build_sync_command("/local/rec-dir", cfg)
    assert args == [
        "aws",
        "s3",
        "sync",
        "/local/rec-dir",
        "s3://my-bucket/recordings/",
        "--profile",
        "acme",
    ]


# --------------------------------------------------------------------------- #
# Gating: skipped when disabled or when s3 is None
# --------------------------------------------------------------------------- #
def test_sync_skipped_when_toggle_disabled(monkeypatch):
    cfg = _make_config(s3_sync=False, with_s3=True)
    handle = _FakeHandle()
    recorder = _CmdRecorder(handle)
    monkeypatch.setattr(s3_mod.duct, "cmd", recorder)

    result = s3_mod.sync("/local/rec-dir", cfg)

    assert result is False
    assert recorder.calls == []  # no child process spawned
    assert handle.polled == 0


def test_sync_skipped_when_s3_config_none(monkeypatch):
    cfg = _make_config(s3_sync=True, with_s3=False)
    handle = _FakeHandle()
    recorder = _CmdRecorder(handle)
    monkeypatch.setattr(s3_mod.duct, "cmd", recorder)

    result = s3_mod.sync("/local/rec-dir", cfg)

    assert result is False
    assert recorder.calls == []


def test_is_enabled_matrix():
    assert s3_mod.is_enabled(_make_config(s3_sync=True, with_s3=True)) is True
    assert s3_mod.is_enabled(_make_config(s3_sync=False, with_s3=True)) is False
    assert s3_mod.is_enabled(_make_config(s3_sync=True, with_s3=False)) is False


def test_build_sync_command_requires_s3():
    cfg = _make_config(s3_sync=True, with_s3=False)
    with pytest.raises(ValueError):
        s3_mod.build_sync_command("/local/rec-dir", cfg)


# --------------------------------------------------------------------------- #
# Failure + timeout paths
# --------------------------------------------------------------------------- #
def test_sync_raises_on_nonzero_status(monkeypatch):
    cfg = _make_config(s3_sync=True, with_s3=True)
    handle = _FakeHandle(status=2)
    recorder = _CmdRecorder(handle)
    monkeypatch.setattr(s3_mod.duct, "cmd", recorder)

    with pytest.raises(s3_mod.S3SyncError):
        s3_mod.sync("/local/rec-dir", cfg)


def test_sync_times_out_and_kills(monkeypatch):
    # Timeout of 0 seconds: the deadline is immediately in the past, and the
    # fake handle never completes, so the first poll() -> None triggers a kill.
    cfg = _make_config(s3_sync=True, with_s3=True, s3_timeout=0)
    handle = _FakeHandle(complete_immediately=False)
    recorder = _CmdRecorder(handle)
    monkeypatch.setattr(s3_mod.duct, "cmd", recorder)

    with pytest.raises(s3_mod.S3SyncTimeout):
        s3_mod.sync("/local/rec-dir", cfg)

    assert handle.killed is True
    assert handle.waited is True
