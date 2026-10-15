"""Tests for write_model.py - dataclasses, tag algebra, sanitiser, embed registry.

Zero Qt import anywhere in this file. Source stays ASCII: special characters are
spelled as escapes / `\\N{...}` names, and printed strings go through `ascii()`.
"""

import sys
from pathlib import Path

_MVP_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_MVP_ROOT / "SAP Modifier"))

import pytest

import write_testkit
from write_model import (
    MAX_INDENT,
    MAX_RUN_TEXT,
    SPREAD_MAX,
    SPREAD_MIN,
    EmbedRegistry,
    EmbedRun,
    Paragraph,
    Run,
    WriteDocument,
    apply_spread,
    clamp_indent,
    coalesce_runs,
    next_italic,
    normalize_tags,
    sanitize_text,
    toggle_target,
)

OBJ = "\N{OBJECT REPLACEMENT CHARACTER}"
LS = "\N{LINE SEPARATOR}"
PS = "\N{PARAGRAPH SEPARATOR}"
FFFD = "\N{REPLACEMENT CHARACTER}"


def test_normalize_tags_keeps_only_canonical_values():
    messy = {"bold": True, "line": False, "dash": None, "italic": "right", "spread": 0.0, "junk": 1}
    assert normalize_tags(messy) == {"bold": True, "italic": "right"}
    # canonical key order, so serialised files are deterministic
    assert list(normalize_tags({"spread": 2.5, "italic": "left", "dash": True, "bold": True, "line": True})) == [
        "bold", "line", "dash", "italic", "spread",
    ]
    assert normalize_tags({"italic": "diagonal"}) == {}
    assert normalize_tags({"italic": False}) == {}
    assert normalize_tags({"bold": 1, "line": "yes"}) == {}  # only a real True counts
    assert normalize_tags({"spread": 999}) == {"spread": SPREAD_MAX}
    assert normalize_tags({"spread": -9}) == {"spread": SPREAD_MIN}
    assert normalize_tags({"spread": 2.126}) == {"spread": 2.13}
    for bad in (True, float("nan"), float("inf"), "3", None, [1]):
        assert normalize_tags({"spread": bad}) == {}, ascii(bad)
    assert normalize_tags(None) == {} and normalize_tags([("bold", True)]) == {}


def test_italic_cycle_and_toggle_rule():
    assert next_italic(None) == "right"
    assert next_italic("right") == "left"
    assert next_italic("left") is None
    assert next_italic("garbage") == "right"
    state = None
    seen = []
    for _ in range(4):
        state = next_italic(state)
        seen.append(state)
    assert seen == ["right", "left", None, "right"]
    # all on -> off; anything else (including nothing selected) -> on
    assert toggle_target([True, True]) is False
    assert toggle_target([True, False]) is True
    assert toggle_target([False, False]) is True
    assert toggle_target([]) is True
    assert toggle_target(x for x in (True,)) is False  # any iterable


def test_spread_and_indent_clamps():
    assert apply_spread(None, 1) == 1.0
    assert apply_spread(0, -5) == SPREAD_MIN
    assert apply_spread(31.5, 5) == SPREAD_MAX
    assert apply_spread(1.0, 0.126) == 1.13
    zero = apply_spread(0.5, -0.5)
    assert zero == 0.0 and str(zero) == "0.0"  # never "-0.0"
    assert clamp_indent(-5) == 0 and clamp_indent(3) == 3 and clamp_indent(999) == MAX_INDENT


def test_sanitize_text_scrubs_controls_and_handles_newlines():
    assert sanitize_text("plain text") == "plain text"
    assert sanitize_text("a\x00b\x07c\x7fd") == "abcd"
    assert sanitize_text("tab\tkept") == "tab\tkept"
    assert sanitize_text(f"x{OBJ}y") == "xy"  # the embed placeholder is never real text
    assert sanitize_text("a\ud800b") == f"a{FFFD}b"  # a lone surrogate cannot be saved as UTF-8
    # run-text rule (default): newline-like characters become one space
    for nl in ("\n", "\r", "\r\n", "\v", "\f", LS, PS, "\x85"):
        assert sanitize_text(f"a{nl}b") == "a b", ascii(nl)
    # paste rule: they become "\n" (CRLF counts once) so paragraphs still split
    for nl in ("\n", "\r", "\r\n", "\v", "\f", LS, PS, "\x85"):
        assert sanitize_text(f"a{nl}b", keep_newlines=True) == "a\nb", ascii(nl)
    assert sanitize_text("a\r\n\r\nb", keep_newlines=True) == "a\n\nb"
    # a change is detectable by comparison
    assert sanitize_text("clean") == "clean" and sanitize_text("d\x01irty") != "d\x01irty"


def test_coalesce_runs_merges_equal_tags_and_never_crosses_an_embed():
    png = write_testkit.fake_png_header(4, 4)
    embed = EmbedRun(png, [Run("src", {})])
    runs = [
        Run("a", {}),
        Run("b", {"bold": False}),  # non-canonical off-value: same as untagged
        Run("", {"bold": True}),  # empty text dropped
        Run("c", {"bold": True}),
        Run("d", {"bold": True, "junk": 1}),
        embed,
        Run("e", {"bold": True}),
        Run("f", {"bold": True}),
    ]
    original = [Run(r.text, dict(r.tags)) if isinstance(r, Run) else r for r in runs]
    merged = coalesce_runs(runs)
    assert merged == [Run("ab", {}), Run("cd", {"bold": True}), embed, Run("ef", {"bold": True})]
    assert merged[2] is embed
    # the input list and its runs are not mutated
    assert [(r.text, r.tags) for r in runs if isinstance(r, Run)] == [(r.text, r.tags) for r in original if isinstance(r, Run)]
    # a merged run never exceeds the per-run cap the loader enforces
    big = coalesce_runs([Run("x" * (MAX_RUN_TEXT // 2 + 1)), Run("y" * (MAX_RUN_TEXT // 2 + 1))])
    assert len(big) == 2 and all(len(r.text) <= MAX_RUN_TEXT for r in big)
    assert coalesce_runs([]) == []


def test_outline_and_plain_text_export():
    png = write_testkit.fake_png_header(4, 4)
    doc = WriteDocument(
        [
            Paragraph(0, [Run("Phrase: ", {"bold": True}), Run("@sub/a.txt")]),
            Paragraph(2, [EmbedRun(png, [Run("Goals: ", {}), Run("x", {"line": True})])]),
            Paragraph(1, []),
        ]
    )
    assert doc.outline() == [(0, "Phrase: @sub/a.txt"), (2, "Goals: x"), (1, "")]
    assert doc.to_plain_text() == "Phrase: @sub/a.txt\nGoals: x\n"
    assert doc.to_plain_text(indent_width=2) == "Phrase: @sub/a.txt\n    Goals: x\n  "
    assert doc.version == 1
    # a document is never "no lines"
    assert len(WriteDocument().paragraphs) == 1
    assert len(WriteDocument([]).paragraphs) == 1
    assert WriteDocument().outline() == [(0, "")]


def test_embed_registry_dedupes_hashes_source_and_enforces_caps():
    reg = EmbedRegistry()
    png = write_testkit.fake_png_header(5, 5)
    first = reg.add(png, [Run("hello", {"bold": True})])
    assert len(first) == 40 and int(first, 16) >= 0
    assert reg.add(png, [Run("hello", {"bold": True})]) == first  # identical content dedupes
    assert len(reg) == 1 and reg.total_bytes == len(png) and first in reg
    # same picture, different underlying text -> a different id (no spoofed source sharing)
    other = reg.add(png, [Run("different", {})])
    assert other != first and len(reg) == 2
    # get returns a COPY; mutating it does not change what is stored
    got = reg.get(first)
    assert got.png == png and got.source == [Run("hello", {"bold": True})]
    got.source[0].text = "tampered"
    assert reg.get(first).source[0].text == "hello"
    assert reg.get("0" * 40) is None
    # caps
    small = EmbedRegistry(max_embeds=2)
    small.add(b"a" * 40, [])
    small.add(b"b" * 40, [])
    with pytest.raises(ValueError):
        small.add(b"c" * 40, [])
    small.add(b"a" * 40, [])  # a duplicate is still fine at the cap
    budget = EmbedRegistry(max_total_bytes=10)
    budget.add(b"123456", [])
    with pytest.raises(ValueError):
        budget.add(b"abcdef", [])
    assert budget.total_bytes == 6
    reg.clear()
    assert len(reg) == 0 and reg.total_bytes == 0 and reg.get(first) is None
