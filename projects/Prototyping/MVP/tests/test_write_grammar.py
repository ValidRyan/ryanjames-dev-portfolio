"""Tests for write_grammar.py - line classes, counts, the scope tree, completion helpers.

Zero Qt import anywhere in this file. Source stays ASCII (special characters
are `\\N{...}` names / escapes) and printed strings go through `ascii()`.
"""

import inspect
import sys
from pathlib import Path

_MVP_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_MVP_ROOT / "SAP Modifier"))

import write_grammar
from write_grammar import (
    INSTRUCTION_NAMES,
    INSTRUCTIONS,
    LineCounts,
    classify_line,
    count_lines,
    keyword_prefix_before_cursor,
    matching_keywords,
    parse_scope_tree,
)


def _shape(nodes):
    """`[(line, keyword, structural, [children...]), ...]` for easy comparison."""
    return [(n.line, n.keyword, n.structural, _shape(n.children)) for n in nodes]


def test_instruction_names_are_the_octagon_and_the_registry_matches():
    assert INSTRUCTION_NAMES == (
        "Phrase", "Limits", "Meaning", "Goals", "Respond", "Understanding", "Info", "Knowledge",
    )
    assert list(INSTRUCTIONS) == list(INSTRUCTION_NAMES)
    assert all(callable(fn) for fn in INSTRUCTIONS.values())
    assert write_grammar.TEXT_INSTRUCTIONS_CAN_NEST is False


def test_dud_instructions_return_the_bare_not_implemented_message():
    for name, fn in INSTRUCTIONS.items():
        assert fn("some text") == (None, "not implemented yet"), name
        assert fn("") == (None, "not implemented yet")
    # bare: the Run log adds the "<Keyword> (line N, path):" prefix, not the dud
    assert ":" not in INSTRUCTIONS["Phrase"]("x")[1]


def test_all_eight_keywords_are_recognised_case_insensitively():
    for name in INSTRUCTION_NAMES:
        for variant in (name, name.upper(), name.lower(), name.swapcase()):
            info = classify_line(f"{variant}: value")
            assert info.kind == "instruction", ascii(variant)
            assert info.keyword == name  # always the canonical spelling
            assert info.value == "value" and not info.is_ref


def test_the_colon_must_follow_the_keyword_directly_at_line_start():
    assert classify_line("Phrase : x").kind == "noise"
    assert classify_line("Phrase x").kind == "noise"
    assert classify_line(" Phrase: x").kind == "noise"  # leading whitespace is not skipped
    assert classify_line("\tPhrase: x").kind == "noise"
    info = classify_line("Phrase:x")
    assert info.kind == "instruction" and info.value == "x"
    assert classify_line("Phrase::").value == ":"


def test_partial_unknown_and_lookalike_keywords_are_noise():
    for text in (
        "Phras: x", "Phrases: x", "Note: x", "Infos: y", ":", ": x", "Info", "Goals", "plain prose here",
        "Knowledge - x", "\N{KELVIN SIGN}nowledge: x", "Phrase\N{FULLWIDTH COLON} x", "1: one",
    ):
        assert classify_line(text).kind == "noise", ascii(text)
        assert classify_line(text).keyword is None


def test_comment_and_blank_lines():
    assert classify_line("# a note").kind == "comment"
    assert classify_line("#Phrase: @x").kind == "comment"  # a comment never becomes an instruction
    assert classify_line("#").kind == "comment"
    for text in ("", "   ", "\t", " \t "):
        assert classify_line(text).kind == "blank", ascii(text)
    assert classify_line(" # indented hash").kind == "noise"


def test_text_versus_reference_values_and_spans():
    text_line = "Goals: finish the draft"
    info = classify_line(text_line)
    assert (info.kind, info.keyword, info.value, info.is_ref, info.ref_text) == (
        "instruction", "Goals", "finish the draft", False, None,
    )
    assert text_line[slice(*info.kw_span)] == "Goals"
    assert text_line[slice(*info.value_span)] == "finish the draft"
    assert info.ref_span is None

    ref_line = "Phrase:   @ sub/a.txt  "
    ref = classify_line(ref_line)
    assert ref.is_ref and ref.value == "@ sub/a.txt" and ref.ref_text == "sub/a.txt"
    assert ref_line[slice(*ref.kw_span)] == "Phrase"
    assert ref_line[slice(*ref.value_span)] == "@ sub/a.txt"
    assert ref_line[slice(*ref.ref_span)] == "sub/a.txt"

    tight = classify_line("Info:@x")
    assert tight.is_ref and tight.ref_text == "x" and "Info:@x"[slice(*tight.ref_span)] == "x"
    # a leading @ elsewhere in the value is not a reference; there is no escape for a leading @
    assert not classify_line("Info: mail me @ home").is_ref
    empty_ref = classify_line("Info: @")
    assert empty_ref.is_ref and empty_ref.ref_text == "" and empty_ref.ref_span[0] == empty_ref.ref_span[1]


def test_an_empty_value_is_still_a_clarity_instruction():
    for text in ("Limits:", "Limits:   ", "limits: \t"):
        info = classify_line(text)
        assert info.kind == "instruction" and info.keyword == "Limits"
        assert info.value == "" and info.value_span is None and not info.is_ref and info.ref_text is None
    assert count_lines(["Limits:"]).clarity == 1


def test_count_lines_accepts_pairs_or_bare_strings():
    lines = ["Phrase: @a", "Goals: x", "# c", "", "prose", "  ", "Info:", "Bad key: y"]
    assert count_lines(lines) == LineCounts(clarity=3, noise=2, comment=1, blank=2)
    assert count_lines([(0, "Phrase: @a"), (3, "prose"), (1, "# c"), (2, "")]) == LineCounts(1, 1, 1, 1)
    assert count_lines([]) == LineCounts(0, 0, 0, 0)
    assert parse_scope_tree(lines).counts == count_lines(lines)


def test_scope_nesting_dedent_and_outdent_to_an_unseen_level():
    forest = parse_scope_tree(
        [
            (0, "Phrase: @a"),
            (1, "Info: @b"),
            (2, "Goals: x"),
            (1, "Limits: @c"),
            (0, "Meaning: @d"),
        ]
    )
    assert _shape(forest.roots) == [
        (1, "Phrase", None, [(2, "Info", None, [(3, "Goals", None, [])]), (4, "Limits", None, [])]),
        (5, "Meaning", None, []),
    ]
    phrase, meaning = forest.roots
    assert phrase.parent is None and phrase.children[0].parent is phrase
    assert phrase.children[0].children[0].parent is phrase.children[0]
    assert forest.diagnostics == []
    # Outdent to a level never seen (3 -> 2 after 0,3) lands on the nearest shallower node.
    forest = parse_scope_tree([(0, "Phrase: @a"), (3, "Info: @b"), (2, "Limits: @c"), (1, "Goals: @d"), (5, "Respond: @e")])
    phrase = forest.roots[0]
    assert [c.keyword for c in phrase.children] == ["Info", "Limits", "Goals"]
    assert phrase.children[2].children[0].keyword == "Respond"
    # dedent to top level starts a new root; siblings at the same indent are siblings
    forest = parse_scope_tree([(0, "Phrase: @a"), (1, "Info: @b"), (1, "Limits: @c"), (0, "Goals: @d")])
    assert [r.keyword for r in forest.roots] == ["Phrase", "Goals"]
    assert [c.keyword for c in forest.roots[0].children] == ["Info", "Limits"]


def test_indent_jump_and_first_line_diagnostics():
    forest = parse_scope_tree([(0, "Phrase: @a"), (3, "Info: @b"), (4, "Goals: @c"), (2, "Limits: @d")])
    codes = [(d.code, d.line, d.subject_line) for d in forest.diagnostics]
    # 0 -> 3 is a legal jump but reported on the shallower line (1); 3 -> 4 is a single step;
    # 4 -> 2 pops back to the Phrase at 0 (a jump of 2 from it), reported on line 1 again.
    assert codes == [("indent_jump", 1, 2), ("indent_jump", 1, 4)]
    assert all(d.message for d in forest.diagnostics)
    assert [c.keyword for c in forest.roots[0].children] == ["Info", "Limits"]

    first = parse_scope_tree([(2, "Phrase: @a"), (2, "Info: @b"), (3, "Goals: @c")])
    assert [(d.code, d.line) for d in first.diagnostics] == [("first_line_indented", 1)]
    assert [r.keyword for r in first.roots] == ["Phrase", "Info"]  # both are top level; no phantom parent
    assert first.roots[1].children[0].keyword == "Goals"
    # a blank/comment first line does not count as "the first line"
    assert parse_scope_tree([(0, ""), (0, "# c"), (0, "Phrase: @a")]).diagnostics == []
    assert [d.code for d in parse_scope_tree([(0, ""), (4, "Phrase: @a")]).diagnostics] == ["first_line_indented"]


def test_blank_and_comment_lines_are_transparent_to_scope():
    forest = parse_scope_tree(
        [
            (0, "Phrase: @a"),
            (0, "# a comment at indent 0 does not close the scope"),
            (0, ""),
            (2, "# deeper comment"),
            (1, "Info: @b"),
            (5, ""),
            (0, "Goals: @c"),
        ]
    )
    assert _shape(forest.roots) == [(1, "Phrase", None, [(5, "Info", None, [])]), (7, "Goals", None, [])]
    assert forest.counts == LineCounts(clarity=3, noise=0, comment=2, blank=2)
    assert forest.diagnostics == []  # transparent lines never trigger jump warnings


def test_children_under_noise_or_text_are_structural_and_the_flag_is_honoured(monkeypatch):
    # under noise: listed under the nearest instruction ancestor (or as a root), never resolved
    forest = parse_scope_tree([(0, "prose line"), (1, "Info: @x"), (2, "Goals: @y")])
    assert _shape(forest.roots) == [(2, "Info", "under_noise", [(3, "Goals", None, [])])]
    forest = parse_scope_tree([(0, "Phrase: @a"), (1, "not an instruction"), (2, "Goals: t")])
    assert _shape(forest.roots) == [(1, "Phrase", None, [(3, "Goals", "under_noise", [])])]
    # noise nodes never appear anywhere in the tree
    assert all(n.keyword for n in _walk(forest.roots))
    # under a text-valued instruction (decision D2): structural, but still listed under it
    forest = parse_scope_tree([(0, "Goals: finish"), (1, "Info: @x")])
    assert _shape(forest.roots) == [(1, "Goals", None, [(2, "Info", "under_text", [])])]
    # an EMPTY value is text-valued too
    assert _shape(parse_scope_tree([(0, "Goals:"), (1, "Info: @x")]).roots)[0][3][0][2] == "under_text"
    # under a reference (whatever it points at, decided later by the resolver): allowed
    assert _shape(parse_scope_tree([(0, "Goals: @x"), (1, "Info: t")]).roots)[0][3][0][2] is None
    # the flag flips text nesting on, but never the under-noise rule
    monkeypatch.setattr(write_grammar, "TEXT_INSTRUCTIONS_CAN_NEST", True)
    forest = parse_scope_tree([(0, "Goals: finish"), (1, "Info: @x"), (0, "prose"), (1, "Limits: @y")])
    assert _shape(forest.roots) == [(1, "Goals", None, [(2, "Info", None, [])]), (4, "Limits", "under_noise", [])]


def _walk(nodes):
    stack = list(nodes)
    while stack:
        node = stack.pop()
        yield node
        stack.extend(node.children)


def test_scope_uses_indent_ints_not_whitespace_and_two_thousand_deep_input_is_iterative():
    # leading whitespace is NOT indentation: these are noise lines, the ints decide scope
    forest = parse_scope_tree([(0, "Phrase: @a"), (0, "    Info: @b"), (0, "\tGoals: @c")])
    assert _shape(forest.roots) == [(1, "Phrase", None, [])]
    assert forest.counts.noise == 2
    forest = parse_scope_tree(["Phrase: @a", "Info: @b"])  # bare strings are indent 0
    assert [r.keyword for r in forest.roots] == ["Phrase", "Info"]

    # 2000 levels deep must not recurse (run with the recursion limit nearly exhausted)
    lines = [(i, "Phrase: @x") for i in range(2000)] + [(0, "Goals: @end")]
    old_limit = sys.getrecursionlimit()
    sys.setrecursionlimit(len(inspect.stack(0)) + 40)
    try:
        forest = parse_scope_tree(lines)
        depth, node = 1, forest.roots[0]
        while node.children:
            node = node.children[0]
            depth += 1
        total = sum(1 for _ in _walk(forest.roots))
        no_diagnostics = len(forest.diagnostics) == 0  # every step is +1
    finally:
        sys.setrecursionlimit(old_limit)
    assert total == 2001 and no_diagnostics
    assert depth == 2000
    assert [r.keyword for r in forest.roots] == ["Phrase", "Goals"]
    assert repr(forest.roots[0]).startswith("ScopeNode(")  # repr/eq must not recurse either


def test_keyword_prefix_and_matching_keywords_helpers():
    assert keyword_prefix_before_cursor("") == ""
    assert keyword_prefix_before_cursor("Ph") == "Ph"
    assert keyword_prefix_before_cursor("phr") == "phr"
    for none in ("Phrase:", "Ph ", " Ph", "1P", "Ph1", "Phr\u00e9", "# P", "Goals: Ph"):
        assert keyword_prefix_before_cursor(none) == "", ascii(none)
    assert matching_keywords("i") == ["Info"]
    assert matching_keywords("K") == ["Knowledge"]
    assert matching_keywords("PH") == ["Phrase"]
    assert matching_keywords("U") == ["Understanding"]
    assert matching_keywords("l") == ["Limits"]
    assert matching_keywords("Understanding") == ["Understanding"]
    assert matching_keywords("") == [] and matching_keywords("Zz") == [] and matching_keywords("Phrases") == []
    assert matching_keywords("\N{KELVIN SIGN}") == []
    assert [k for name in "PLMGRUIK" for k in matching_keywords(name)] == list(INSTRUCTION_NAMES)
