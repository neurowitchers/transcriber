"""S3 sync stage (``publish/s3.py``).

Uploads a recording directory to S3 via ``aws s3 sync``, executed as a child
process through the :mod:`duct` library. The invocation is:

    aws s3 sync <local_dir> <config.s3.bucket> --profile <config.s3.profile>

The call is wrapped with a configurable execution timeout (``config.timeouts.s3``,
requirement E3) so a hung network socket cannot block the orchestrator
indefinitely.

The stage is *gated*: it is a no-op (returns ``False``) when
``config.stages.s3_sync`` is falsy or when ``config.s3`` is ``None``.

Ordering contract (requirement R12): a successful ``sync`` MUST complete before
any cleanup/deletion of intermediates. This module only performs the upload;
:mod:`transcriber.cleanup` is a separate function the orchestrator calls
*after* :func:`sync` returns ``True`` (or after it is skipped on a run where
S3 is disabled).
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Union

import duct

from transcriber.config import Config

# How often (seconds) we poll the child process while enforcing the timeout.
_POLL_INTERVAL_SECONDS = 0.1


class S3SyncError(RuntimeError):
    """Raised when the ``aws s3 sync`` child process fails or times out."""


class S3SyncTimeout(S3SyncError):
    """Raised when ``aws s3 sync`` exceeds ``config.timeouts.s3`` seconds."""


def build_sync_command(local_dir: Union[str, Path], config: Config) -> list[str]:
    """Return the ``aws s3 sync`` argument vector for ``local_dir``.

    Raises:
        ValueError: if ``config.s3`` is ``None`` (caller must gate on this).
    """
    if config.s3 is None:
        raise ValueError("build_sync_command requires config.s3 to be set")
    return [
        "aws",
        "s3",
        "sync",
        str(local_dir),
        config.s3.bucket,
        "--profile",
        config.s3.profile,
    ]


def is_enabled(config: Config) -> bool:
    """Return ``True`` when the S3 sync stage should run for ``config``.

    The stage runs only when both the toggle is on (``config.stages.s3_sync``)
    and an S3 target is configured (``config.s3 is not None``).
    """
    return bool(config.stages.s3_sync) and config.s3 is not None


def sync(local_dir: Union[str, Path], config: Config) -> bool:
    """Sync ``local_dir`` to the configured S3 bucket via ``aws s3 sync``.

    Gated on :func:`is_enabled`. When the stage is disabled (toggle off or no
    ``config.s3``), this is a no-op and returns ``False`` without invoking any
    child process.

    On an enabled run it executes ``aws s3 sync`` through :mod:`duct`, enforcing
    ``config.timeouts.s3`` seconds. Returns ``True`` on a successful upload.

    Raises:
        S3SyncTimeout: if the upload exceeds the configured timeout.
        S3SyncError: if the ``aws`` process exits non-zero.
    """
    if not is_enabled(config):
        return False

    args = build_sync_command(local_dir, config)
    timeout = config.timeouts.s3

    handle = duct.cmd(*args).start()
    deadline = time.monotonic() + timeout
    try:
        while True:
            output = handle.poll()
            if output is not None:
                # Completed within the timeout. duct raises on non-zero status
                # via wait()/poll(), but guard defensively for unchecked cmds.
                if getattr(output, "status", 0) not in (0, None):
                    raise S3SyncError(
                        f"aws s3 sync failed with status {output.status}"
                    )
                return True
            if time.monotonic() >= deadline:
                handle.kill()
                handle.wait()
                raise S3SyncTimeout(
                    f"aws s3 sync exceeded timeout of {timeout}s"
                )
            time.sleep(_POLL_INTERVAL_SECONDS)
    except duct.StatusError as exc:  # non-zero exit surfaced by duct
        raise S3SyncError(str(exc)) from exc
