"""Packaging guard: the prompt templates shipped inside the ``transcriber``
package must be present and readable (critique E6).

These Markdown templates are package data under ``transcriber/prompt_templates/``.
``hatchling`` includes them via ``packages = ["transcriber"]`` in any wheel
built from source (including git-ref installs), so a refactor that moved or
dropped them would silently break the slides/summary stages at runtime. This
test fails fast if that happens.
"""

from __future__ import annotations

from transcriber import agent as agent_mod

EXPECTED_TEMPLATES = ("slide_extractor.md", "summary.md")


def test_prompt_templates_dir_exists():
    assert agent_mod._TEMPLATES_DIR.is_dir()


def test_expected_prompt_templates_present_and_readable():
    for name in EXPECTED_TEMPLATES:
        path = agent_mod._TEMPLATES_DIR / name
        assert path.is_file(), f"missing packaged template: {name}"
        assert path.read_text(encoding="utf-8").strip(), f"empty template: {name}"
