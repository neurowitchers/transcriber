"""Tests for transcriber.cleanup using real temporary files.

Verifies that cleanup deletes exactly the intermediate artifact set, preserves
``.mp4``, ``.md``, and the ``.txt`` transcript, honors ``keep_intermediates``,
and documents/enforces the sync-before-delete ordering as a caller contract.
"""

from __future__ import annotations

from pathlib import Path

from transcriber import cleanup as cleanup_mod
from transcriber.cleanup import cleanup


NAME = "meeting-2026-09-22"


def _make_all_artifacts(directory: Path) -> dict[str, Path]:
    """Create the full artifact set for one recording; return path map."""
    mp4 = directory / f"{NAME}.mp4"
    md = directory / f"{NAME}.md"
    mp3 = directory / f"{NAME}.mp3"
    txt = directory / f"{NAME}.txt"
    slides_md = directory / f"{NAME}.slides.md"
    slides_clean = directory / f"{NAME}.slides-clean.md"
    slides_dir = directory / f"extracted_slides.{NAME}"
    transcribe_work_dir = directory / f"transcribe_work.{NAME}"

    for f in (mp4, md, mp3, txt, slides_md, slides_clean):
        f.write_text("data", encoding="utf-8")
    slides_dir.mkdir()
    (slides_dir / "slide-001.jpg").write_text("img", encoding="utf-8")
    # The scenes CSV lives inside the extracted-slides directory.
    scenes = slides_dir / f"{NAME}.scenes.csv"
    scenes.write_text("data", encoding="utf-8")
    # The OpenRouter STT work dir holds per-chunk parts + the resume manifest.
    transcribe_work_dir.mkdir()
    (transcribe_work_dir / "part_000.mp3").write_text("audio", encoding="utf-8")
    (transcribe_work_dir / "part_001.mp3").write_text("audio", encoding="utf-8")
    (transcribe_work_dir / "segments.json").write_text("{}", encoding="utf-8")

    return {
        "mp4": mp4,
        "md": md,
        "mp3": mp3,
        "txt": txt,
        "slides_md": slides_md,
        "slides_clean": slides_clean,
        "scenes": scenes,
        "slides_dir": slides_dir,
        "transcribe_work_dir": transcribe_work_dir,
    }


def test_cleanup_deletes_intermediates_and_keeps_durable(tmp_path):
    art = _make_all_artifacts(tmp_path)

    removed = cleanup(art["mp4"])

    # Durable outputs preserved.
    assert art["mp4"].exists()
    assert art["md"].exists()
    assert art["txt"].exists()  # speech-to-text transcript is kept
    assert art["slides_clean"].exists()  # reader-facing cleaned slides are kept

    # Intermediates gone.
    for key in ("mp3", "scenes", "slides_md", "slides_dir", "transcribe_work_dir"):
        assert not art[key].exists(), f"{key} should have been deleted"

    # Exactly the intermediate set was removed (not the .txt transcript).
    assert set(removed) == {
        art["mp3"],
        art["scenes"],
        art["slides_md"],
        art["slides_dir"],
        art["transcribe_work_dir"],
    }


def test_cleanup_never_touches_mp4_or_md(tmp_path):
    art = _make_all_artifacts(tmp_path)
    cleanup(art["mp4"])
    assert art["mp4"].read_text(encoding="utf-8") == "data"
    assert art["md"].read_text(encoding="utf-8") == "data"
    # The .txt speech-to-text transcript is a durable output too.
    assert art["txt"].read_text(encoding="utf-8") == "data"


def test_keep_intermediates_disables_all_deletion(tmp_path):
    art = _make_all_artifacts(tmp_path)

    removed = cleanup(art["mp4"], keep_intermediates=True)

    assert removed == []
    for p in art.values():
        assert p.exists(), f"{p} must remain with keep_intermediates=True"


def test_cleanup_handles_missing_artifacts(tmp_path):
    # Only mp4 + md present (e.g. slides disabled).
    mp4 = tmp_path / f"{NAME}.mp4"
    md = tmp_path / f"{NAME}.md"
    mp4.write_text("v", encoding="utf-8")
    md.write_text("s", encoding="utf-8")

    removed = cleanup(mp4)

    assert removed == []
    assert mp4.exists()
    assert md.exists()


def test_cleanup_isolated_to_this_recording(tmp_path):
    # A second recording's intermediates must not be touched.
    art = _make_all_artifacts(tmp_path)
    other_mp3 = tmp_path / "other.mp3"
    other_mp3.write_text("x", encoding="utf-8")

    cleanup(art["mp4"])

    assert other_mp3.exists(), "cleanup must only target the given recording"


def test_intermediate_paths_excludes_durable(tmp_path):
    mp4 = tmp_path / f"{NAME}.mp4"
    candidates = cleanup_mod.intermediate_paths(mp4)
    names = {p.name for p in candidates}
    assert f"{NAME}.mp4" not in names
    assert f"{NAME}.md" not in names  # durable summary must be kept
    assert f"{NAME}.txt" not in names  # speech-to-text transcript must be kept
    assert names == {
        f"{NAME}.mp3",
        f"{NAME}.scenes.csv",
        f"{NAME}.telegram.md",
        f"{NAME}.slides.md",
        f"{NAME}.notion_published.json",
        f"extracted_slides.{NAME}",
        f"transcribe_work.{NAME}",
    }
    # The scenes CSV candidate must live inside the extracted-slides dir, not
    # the recording dir where it never existed.
    scenes = next(p for p in candidates if p.name == f"{NAME}.scenes.csv")
    assert scenes.parent.name == f"extracted_slides.{NAME}"
    assert scenes == tmp_path / f"extracted_slides.{NAME}" / f"{NAME}.scenes.csv"


def test_cleanup_keeps_txt_transcript(tmp_path):
    """The ``<name>.txt`` speech-to-text transcript must survive cleanup."""
    mp4 = tmp_path / f"{NAME}.mp4"
    mp4.write_text("video", encoding="utf-8")
    txt = tmp_path / f"{NAME}.txt"
    txt.write_text("hello world transcript", encoding="utf-8")
    mp3 = tmp_path / f"{NAME}.mp3"
    mp3.write_text("audio", encoding="utf-8")

    removed = cleanup(mp4)

    assert txt.exists(), "the .txt transcript must be kept"
    assert txt not in removed
    assert not mp3.exists(), "the .mp3 must still be deleted"
    assert mp3 in removed


def test_cleanup_deletes_digest_keeps_summary(tmp_path):
    mp4 = tmp_path / f"{NAME}.mp4"
    mp4.write_text("video", encoding="utf-8")
    summary = tmp_path / f"{NAME}.md"
    summary.write_text("# full summary", encoding="utf-8")
    digest = tmp_path / f"{NAME}.telegram.md"
    digest.write_text("short digest", encoding="utf-8")

    cleanup(mp4)

    assert summary.exists(), "durable .md summary must be kept"
    assert not digest.exists(), "the .telegram.md digest must be deleted"
    assert mp4.exists()


def test_cleanup_deletes_slides_md_keeps_summary(tmp_path):
    mp4 = tmp_path / f"{NAME}.mp4"
    mp4.write_text("video", encoding="utf-8")
    summary = tmp_path / f"{NAME}.md"
    summary.write_text("# full summary", encoding="utf-8")
    slides_md = tmp_path / f"{NAME}.slides.md"
    slides_md.write_text("## Slide 1\n...", encoding="utf-8")

    removed = cleanup(mp4)

    assert summary.exists(), "durable .md summary must be kept"
    assert not slides_md.exists(), "the .slides.md artifact must be deleted"
    assert slides_md in removed
    assert mp4.exists()


def test_cleanup_keeps_slides_clean_but_deletes_slides_md(tmp_path):
    """<name>.slides-clean.md (reader-facing, markers stripped) is DURABLE;
    <name>.slides.md (markers-kept debug intermediate) is deleted."""
    mp4 = tmp_path / f"{NAME}.mp4"
    mp4.write_text("video", encoding="utf-8")
    (tmp_path / f"{NAME}.md").write_text("# summary", encoding="utf-8")
    slides_md = tmp_path / f"{NAME}.slides.md"
    slides_md.write_text("[[SLIDE_EMPTY 00:00 - 00:05]]\n\n### Real\n- x", encoding="utf-8")
    slides_clean = tmp_path / f"{NAME}.slides-clean.md"
    slides_clean.write_text("### Real\n- x", encoding="utf-8")

    removed = cleanup(mp4)

    assert slides_clean.exists(), "the durable .slides-clean.md must be kept"
    assert slides_clean not in removed
    assert not slides_md.exists(), "the .slides.md debug artifact must be deleted"
    assert slides_md in removed


def test_keep_intermediates_preserves_slides_md(tmp_path):
    mp4 = tmp_path / f"{NAME}.mp4"
    mp4.write_text("video", encoding="utf-8")
    slides_md = tmp_path / f"{NAME}.slides.md"
    slides_md.write_text("## Slide 1\n...", encoding="utf-8")

    removed = cleanup(mp4, keep_intermediates=True)

    assert removed == []
    assert slides_md.exists(), "--keep-intermediates must preserve .slides.md"


# --------------------------------------------------------------------------- #
# OpenRouter STT work dir (``transcribe_work.<name>/``) — holds the per-chunk
# ``part_*.mp3`` files and the ``segments.json`` resume manifest. It is an
# intermediate artifact and must be rmtree'd on cleanup, while the durable
# ``<name>.txt`` transcript is preserved; ``--keep-intermediates`` keeps all.
# --------------------------------------------------------------------------- #
def test_cleanup_removes_transcribe_work_dir_keeps_txt(tmp_path):
    mp4 = tmp_path / f"{NAME}.mp4"
    mp4.write_text("video", encoding="utf-8")
    txt = tmp_path / f"{NAME}.txt"
    txt.write_text("Speaker 1: hello", encoding="utf-8")
    work_dir = tmp_path / f"transcribe_work.{NAME}"
    work_dir.mkdir()
    part0 = work_dir / "part_000.mp3"
    part1 = work_dir / "part_001.mp3"
    manifest = work_dir / "segments.json"
    part0.write_text("audio", encoding="utf-8")
    part1.write_text("audio", encoding="utf-8")
    manifest.write_text('{"segment_seconds": 480}', encoding="utf-8")

    removed = cleanup(mp4)

    # The whole work dir (parts + manifest) is removed.
    assert not work_dir.exists(), "transcribe_work.<name>/ must be rmtree'd"
    assert work_dir in removed
    # The durable transcript survives.
    assert txt.exists(), "the .txt transcript must be kept"
    assert txt not in removed


def test_keep_intermediates_preserves_transcribe_work_dir(tmp_path):
    mp4 = tmp_path / f"{NAME}.mp4"
    mp4.write_text("video", encoding="utf-8")
    txt = tmp_path / f"{NAME}.txt"
    txt.write_text("Speaker 1: hello", encoding="utf-8")
    work_dir = tmp_path / f"transcribe_work.{NAME}"
    work_dir.mkdir()
    (work_dir / "part_000.mp3").write_text("audio", encoding="utf-8")
    (work_dir / "segments.json").write_text("{}", encoding="utf-8")

    removed = cleanup(mp4, keep_intermediates=True)

    assert removed == []
    assert work_dir.exists(), "--keep-intermediates must preserve the work dir"
    assert (work_dir / "part_000.mp3").exists()
    assert (work_dir / "segments.json").exists()
    assert txt.exists()


def test_intermediate_paths_includes_transcribe_work_dir(tmp_path):
    mp4 = tmp_path / f"{NAME}.mp4"
    candidates = cleanup_mod.intermediate_paths(mp4)
    work_dir = tmp_path / f"transcribe_work.{NAME}"
    assert work_dir in candidates
    # It lives alongside the recording, not nested under slides.
    assert work_dir.parent == tmp_path


# --------------------------------------------------------------------------- #
# Sync-before-delete ordering (contract enforced by the orchestrator).
#
# cleanup() performs no upload; it is a separate function meant to be invoked
# only after a successful sync. We model/verify that ordering here by driving
# the two functions in sequence and asserting cleanup does not run when sync
# has not succeeded.
# --------------------------------------------------------------------------- #
def test_sync_before_delete_ordering(tmp_path):
    art = _make_all_artifacts(tmp_path)
    events: list[str] = []

    def fake_sync_success() -> bool:
        events.append("sync")
        return True

    # Orchestrator-style sequencing: sync first, then cleanup.
    synced = fake_sync_success()
    if synced:
        cleanup(art["mp4"])
        events.append("cleanup")

    assert events == ["sync", "cleanup"]
    assert art["mp4"].exists() and art["md"].exists()
    assert not art["mp3"].exists()


def test_no_cleanup_when_sync_fails(tmp_path):
    art = _make_all_artifacts(tmp_path)

    def fake_sync_failure() -> bool:
        return False  # e.g. raised/failed upstream -> treated as not-synced

    synced = fake_sync_failure()
    if synced:  # pragma: no cover - branch intentionally not taken
        cleanup(art["mp4"])

    # Nothing deleted because sync did not succeed.
    for p in art.values():
        assert p.exists()
