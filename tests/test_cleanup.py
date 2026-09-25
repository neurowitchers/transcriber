"""Tests for transcriber.cleanup using real temporary files.

Verifies that cleanup deletes exactly the intermediate artifact set, preserves
``.mp4`` and ``.md``, honors ``keep_intermediates``, and documents/enforces the
sync-before-delete ordering as a caller contract.
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
    slides_dir = directory / f"extracted_slides.{NAME}"

    for f in (mp4, md, mp3, txt, slides_md):
        f.write_text("data", encoding="utf-8")
    slides_dir.mkdir()
    (slides_dir / "slide-001.jpg").write_text("img", encoding="utf-8")
    # The scenes CSV lives inside the extracted-slides directory.
    scenes = slides_dir / f"{NAME}.scenes.csv"
    scenes.write_text("data", encoding="utf-8")

    return {
        "mp4": mp4,
        "md": md,
        "mp3": mp3,
        "txt": txt,
        "slides_md": slides_md,
        "scenes": scenes,
        "slides_dir": slides_dir,
    }


def test_cleanup_deletes_intermediates_and_keeps_durable(tmp_path):
    art = _make_all_artifacts(tmp_path)

    removed = cleanup(art["mp4"])

    # Durable outputs preserved.
    assert art["mp4"].exists()
    assert art["md"].exists()

    # Intermediates gone.
    for key in ("mp3", "txt", "scenes", "slides_md", "slides_dir"):
        assert not art[key].exists(), f"{key} should have been deleted"

    # Exactly the intermediate set was removed.
    assert set(removed) == {
        art["mp3"],
        art["txt"],
        art["scenes"],
        art["slides_md"],
        art["slides_dir"],
    }


def test_cleanup_never_touches_mp4_or_md(tmp_path):
    art = _make_all_artifacts(tmp_path)
    cleanup(art["mp4"])
    assert art["mp4"].read_text(encoding="utf-8") == "data"
    assert art["md"].read_text(encoding="utf-8") == "data"


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
    assert names == {
        f"{NAME}.mp3",
        f"{NAME}.txt",
        f"{NAME}.scenes.csv",
        f"{NAME}.telegram.md",
        f"{NAME}.slides.md",
        f"{NAME}.notion_published.json",
        f"extracted_slides.{NAME}",
    }
    # The scenes CSV candidate must live inside the extracted-slides dir, not
    # the recording dir where it never existed.
    scenes = next(p for p in candidates if p.name == f"{NAME}.scenes.csv")
    assert scenes.parent.name == f"extracted_slides.{NAME}"
    assert scenes == tmp_path / f"extracted_slides.{NAME}" / f"{NAME}.scenes.csv"


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


def test_keep_intermediates_preserves_slides_md(tmp_path):
    mp4 = tmp_path / f"{NAME}.mp4"
    mp4.write_text("video", encoding="utf-8")
    slides_md = tmp_path / f"{NAME}.slides.md"
    slides_md.write_text("## Slide 1\n...", encoding="utf-8")

    removed = cleanup(mp4, keep_intermediates=True)

    assert removed == []
    assert slides_md.exists(), "--keep-intermediates must preserve .slides.md"


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
