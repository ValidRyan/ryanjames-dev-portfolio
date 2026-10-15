"""Tests proving exact/verbatim .docx text extraction."""

import sys
from pathlib import Path

# docx_extract.py lives under MVP/File Recompiler/, and sample/ (an implicit
# namespace package) lives under MVP/ too, as a sibling of tests/ - both are
# reached from the shared MVP/ root and need to be importable regardless of
# how pytest is invoked.
_MVP_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_MVP_ROOT / "File Recompiler"))
sys.path.insert(0, str(_MVP_ROOT))

import docx_extract
from sample.generate_sample import PARAGRAPHS as EXPECTED_PARAGRAPHS

SAMPLE_PATH = _MVP_ROOT / "sample" / "sample_input.docx"

EXPECTED_TEXT = "\n".join(EXPECTED_PARAGRAPHS)


def test_extract_text_matches_expected_paragraphs_exactly():
    result = docx_extract.extract_text(str(SAMPLE_PATH))
    assert result == EXPECTED_TEXT


def test_extract_text_returns_correct_paragraph_count():
    result = docx_extract.extract_text(str(SAMPLE_PATH))
    assert len(result.split("\n")) == len(EXPECTED_PARAGRAPHS)
