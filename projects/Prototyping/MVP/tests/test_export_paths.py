"""Tests for export_paths.py - non-destructive export-path derivation.

Includes tests against the real sample_input.docx/.txt pair on disk under
Prototyping/MVP/sample/, which must NEVER be modified by these tests (all
actual writes happen under tmp_path).
"""

import sys
from pathlib import Path

_MVP_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_MVP_ROOT / "File Recompiler"))
sys.path.insert(0, str(_MVP_ROOT / "Joint Interactor"))

import pytest

import docx_extract
from export_paths import assert_not_original, derive_default_export_path, write_formatted

SAMPLE_DIR = _MVP_ROOT / "sample"


def test_derive_default_export_path_docx_source_is_symmetric():
    docx_path = SAMPLE_DIR / "sample_input.docx"
    assert derive_default_export_path(docx_path) == SAMPLE_DIR / "sample_input.export.docx"


def test_derive_default_export_path_txt_source_is_symmetric():
    txt_path = SAMPLE_DIR / "sample_input.txt"
    assert derive_default_export_path(txt_path) == SAMPLE_DIR / "sample_input.export.txt"


def test_derive_default_export_path_accepts_str_or_path():
    assert derive_default_export_path(str(SAMPLE_DIR / "sample_input.docx")) == (
        SAMPLE_DIR / "sample_input.export.docx"
    )
    assert derive_default_export_path(str(SAMPLE_DIR / "sample_input.txt")) == (
        SAMPLE_DIR / "sample_input.export.txt"
    )


def test_derive_default_export_path_never_collides_with_docx_source():
    docx_path = SAMPLE_DIR / "sample_input.docx"
    assert derive_default_export_path(docx_path) != docx_path


def test_derive_default_export_path_never_collides_with_txt_source():
    txt_path = SAMPLE_DIR / "sample_input.txt"
    assert derive_default_export_path(txt_path) != txt_path


def test_write_formatted_docx_target_produces_real_docx(tmp_path):
    target = tmp_path / "out.docx"
    write_formatted("hello world", target)
    assert docx_extract.extract_text(str(target)) == "hello world"


def test_write_formatted_txt_target_writes_plain_text(tmp_path):
    target = tmp_path / "out.txt"
    write_formatted("hello world", target)
    assert target.read_text(encoding="utf-8") == "hello world"


def test_write_formatted_no_extension_target_writes_plain_text(tmp_path):
    target = tmp_path / "out"
    write_formatted("hello world", target)
    assert target.read_text(encoding="utf-8") == "hello world"


def test_write_formatted_dispatch_is_case_insensitive(tmp_path):
    target = tmp_path / "OUT.DOCX"
    write_formatted("uppercase extension", target)
    assert docx_extract.extract_text(str(target)) == "uppercase extension"


def test_assert_not_original_raises_when_target_matches_a_known_original():
    original = SAMPLE_DIR / "sample_input.docx"
    with pytest.raises(Exception):
        assert_not_original(original, [original])


def test_assert_not_original_raises_regardless_of_str_vs_path_mix():
    original = SAMPLE_DIR / "sample_input.docx"
    with pytest.raises(Exception):
        assert_not_original(str(original), [original])
    with pytest.raises(Exception):
        assert_not_original(original, [str(original)])


def test_assert_not_original_passes_for_a_genuine_export_target():
    original = SAMPLE_DIR / "sample_input.docx"
    export_target = SAMPLE_DIR / "sample_input.export.docx"
    assert assert_not_original(export_target, [original]) is None


def test_assert_not_original_passes_when_no_known_originals():
    target = SAMPLE_DIR / "anything.export.docx"
    assert assert_not_original(target, []) is None
