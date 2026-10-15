"""Tests for the ternary instruction vocabulary (instructions.py)."""

import sys
from pathlib import Path

# Ensure the Prototyping/ root (where instructions.py lives) is importable
# regardless of how pytest is invoked.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "File Recompiler"))

import instructions


def test_equivalate_returns_input_unchanged():
    text = "some text\nwith multiple lines"
    result, message = instructions.equivalate(text)
    assert result == text
    assert message == ""


def test_calculate_single_line_arithmetic():
    result, message = instructions.calculate("2*6")
    assert result == "12"


def test_calculate_multiline_mixed_prose_and_arithmetic():
    text = "This is plain prose text.\n2*6"
    result, message = instructions.calculate(text)
    lines = result.split("\n")
    assert lines[0] == "This is plain prose text."
    assert lines[1] == "12"


def test_calculate_leaves_non_arithmetic_lines_unchanged():
    result, message = instructions.calculate("hello world")
    assert result == "hello world"


def test_calculate_security_rejects_dangerous_expressions():
    dangerous_lines = [
        "__import__('os')",
        "open('x')",
    ]
    for line in dangerous_lines:
        result, message = instructions.calculate(line)
        # Must not execute - the line passes through unchanged, no exception raised.
        assert result == line


def test_calculate_guards_against_pow_blowup_dos():
    # A tiny literal that would otherwise ask for a ~million-digit result.
    line = "9**9999999"
    result, message = instructions.calculate(line)
    # Rejected as "not calculable" (falls back unchanged) rather than hanging
    # or raising - the whole point of the _safe_pow guard.
    assert result == line


def test_copy_paste_cut_return_none_with_message():
    for fn in (
        instructions.copy_instruction,
        instructions.paste_instruction,
        instructions.cut_instruction,
    ):
        result, message = fn("anything")
        assert result is None
        assert len(message) > 0
