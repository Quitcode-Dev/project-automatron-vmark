"""Docs the architect plans from — truncation must be generous and never silent.

The doc reader applied per-file caps of 8000/4000/2000 characters by directory
depth. Against a real spec repo that discarded 70% of a 27KB `docs/PRD.md` and
52% of a 17KB `docs/Charter.md` — the two documents that define the product —
while the small `docs/epics/*.md` and `docs/stories/*.md` files passed whole. The
cut landed mid-sentence with no marker, so the architect could not tell it was
planning from a fragment rather than a document that simply ended there.

Coverage:
  1. A document under the cap is passed through byte-for-byte, with no marker
  2. A document over the cap carries an explicit TRUNCATED marker naming the
     shown and total sizes — the model must be able to tell
  3. The caps are large enough for real specs (regression on the exact file
     sizes in Quitcode-Dev/barthu-method-ylehenkyi-training)
  4. Caps decrease with depth, and the aggregate ceiling exceeds any single file
"""

from __future__ import annotations

import pytest

from orchestrator.orchestrator import (
    _DOC_CHARS_DEEP,
    _DOC_CHARS_NESTED,
    _DOC_CHARS_TOP,
    _DOC_CHARS_TOTAL,
    _format_doc,
)

# Actual byte sizes from the spec repo that surfaced this bug.
PRD_CHARS = 26_978
CHARTER_CHARS = 16_760
EPIC_CHARS = 1_438  # largest of docs/epics/E-001..E-008.md
STORY_CHARS = 1_738  # largest of docs/stories/US-001..US-008.md


def test_document_under_cap_passes_through_untouched() -> None:
    text = "# Charter\n\nEvery byte of this must survive.\n"

    rendered = _format_doc("docs/Charter.md", text, _DOC_CHARS_TOP)

    assert text in rendered
    assert "TRUNCATED" not in rendered
    assert rendered.startswith("\n\n---\n## docs/Charter.md\n\n")


def test_truncation_is_announced_to_the_model() -> None:
    """The whole point: a cut document must not read as a complete one."""
    text = "x" * 100

    rendered = _format_doc("docs/PRD.md", text, 40)

    assert "x" * 40 in rendered
    assert "x" * 41 not in rendered
    assert "TRUNCATED" in rendered
    # Both numbers present so the model knows how much it is missing.
    assert "40" in rendered and "100" in rendered
    assert "docs/PRD.md" in rendered


def test_exact_cap_boundary_is_not_marked_truncated() -> None:
    text = "y" * 500

    rendered = _format_doc("docs/exact.md", text, 500)

    assert "TRUNCATED" not in rendered


@pytest.mark.parametrize(
    "path,size,cap",
    [
        ("docs/PRD.md", PRD_CHARS, _DOC_CHARS_TOP),
        ("docs/Charter.md", CHARTER_CHARS, _DOC_CHARS_TOP),
        ("docs/epics/E-005.md", EPIC_CHARS, _DOC_CHARS_NESTED),
        ("docs/stories/US-005.md", STORY_CHARS, _DOC_CHARS_NESTED),
    ],
)
def test_real_spec_documents_are_not_truncated(path: str, size: int, cap: int) -> None:
    """Regression on the sizes that were actually being cut in production."""
    text = "z" * size

    rendered = _format_doc(path, text, cap)

    assert "TRUNCATED" not in rendered, f"{path} ({size:,} chars) still truncated at {cap:,}"
    assert rendered.count("z") == size


def test_caps_decrease_with_depth_and_fit_within_the_total() -> None:
    assert _DOC_CHARS_TOP >= _DOC_CHARS_NESTED >= _DOC_CHARS_DEEP
    # A single file can never exhaust the aggregate budget on its own, or one
    # large doc would silently starve every doc after it.
    assert _DOC_CHARS_TOTAL > _DOC_CHARS_TOP
