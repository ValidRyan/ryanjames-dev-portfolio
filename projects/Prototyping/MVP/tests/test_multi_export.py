"""Tests for multi_export.py's combined-export formatting."""

import sys
from pathlib import Path

# Ensure the Prototyping/ root (where multi_export.py lives) is importable
# regardless of how pytest is invoked.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "File Recompiler"))

import multi_export


def test_build_combined_text_single_entry():
    result = multi_export.build_combined_text([("a/b/one.docx", "hello")])
    assert result == "--- one.docx ---\nhello"


def test_build_combined_text_multiple_entries_separated_by_blank_line():
    result = multi_export.build_combined_text(
        [("x/one.docx", "first"), ("y/two.docx", "second")]
    )
    assert result == "--- one.docx ---\nfirst\n\n--- two.docx ---\nsecond"


def test_build_combined_text_empty_list():
    assert multi_export.build_combined_text([]) == ""
