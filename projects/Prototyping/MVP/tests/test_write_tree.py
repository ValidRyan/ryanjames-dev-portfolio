"""Tests for write_tree.py - the read-only resolved-tree pane.

`ResolveResult` / `ResolvedNode` values are built DIRECTLY (no files, no
resolver), so every state and kind is reachable and a 2000-deep chain is one loop
here as well: no test helper recurses either (chains are built and walked with
explicit loops, and the deep-chain test runs `show_result` under a tiny recursion
limit). Nothing is painted, so the offscreen platform's missing fonts do not
matter.

Every test runs under the `write_qt` fixture and registers the widget it builds
with `write_testkit.track_qt` (see write_testkit's Qt lifetime rules). Source stays
ASCII; characters outside ASCII are built with `chr()`.
"""

import inspect
import sys
from pathlib import Path

_MVP_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_MVP_ROOT / "SAP Modifier"))

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QAbstractItemView

import write_testkit
import write_tree
from write_grammar import Diagnostic, LineCounts
from write_highlight import REF_STYLES
from write_resolve import ResolvedNode, ResolveResult, badge_text, iter_nodes
from write_testkit import write_qt  # noqa: F401  (fixture: see write_testkit's Qt lifetime rules)
from write_tree import KIND_DATA, ROLE_DATA, STATE_DATA, TreePane

pytestmark = pytest.mark.usefixtures("write_qt")

KINDS = ("text", "write", "store", "doc", "prog", "operator", "folder", "content", "marker")
NON_OK_STATES = ("broken", "blocked", "cycle", "truncated", "unsaved", "structural")
ITEM_FLAGS = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable


def node(role, label, *, kind="text", state="ok", reason="", keyword=None, line=0, ref=None, children=()):
    return ResolvedNode(
        role, label, keyword, "", line, "main.mwrite", kind, state, reason, ref, None, list(children)
    )


def result_for(root, diagnostics=()):
    """A `ResolveResult` whose `nodes` is the true count below `root` (counted iteratively)."""
    count = sum(1 for _ in iter_nodes(root)) - 1
    return ResolveResult(root, count, False, [], list(diagnostics), LineCounts())


def chain(length, leaves=0):
    """root -> n1 -> ... -> n<length> (each the only child), plus `leaves` leaves under the last node."""
    last = node("authored", f"n{length}", keyword="Info", line=length)
    for i in range(leaves):
        last.children.append(node("resolved", f"leaf {i}", kind="content"))
    top = last
    for depth in range(length - 1, 0, -1):
        top = node("authored", f"n{depth}", keyword="Info", line=depth, children=[top])
    return result_for(node("root", "main.mwrite", kind="write", children=[top]))


def items_with_depth(tree):
    """Every item of `tree` with its depth (top-level = 0), pre-order, iteratively."""
    stack = [(tree.topLevelItem(i), 0) for i in range(tree.topLevelItemCount() - 1, -1, -1)]
    while stack:
        item, depth = stack.pop()
        yield item, depth
        for i in range(item.childCount() - 1, -1, -1):
            stack.append((item.child(i), depth + 1))


def make_tree():
    tree = write_testkit.track_qt(TreePane())
    tree.resize(420, 160)
    return tree


def rgb(color):
    return (color.red(), color.green(), color.blue())


def test_badges_and_colours_cover_every_state_and_kind():
    resolved_child = node("resolved", "Info: from the target", keyword="Info", line=1)
    authored_child = node("authored", "Goals: typed under the reference", keyword="Goals", line=3)
    evil = "Info: a" + chr(0x202E) + "b" + chr(0x07) + "c"
    ok_nodes = [
        node("authored", f"{kind} row", kind=kind, keyword="Phrase", line=i + 2, ref=f"ref-{kind}")
        for i, kind in enumerate(KINDS)
    ]
    ok_nodes[1].children = [authored_child, resolved_child]  # the "write" row holds both child roles
    bad_nodes = [
        node("authored", f"{state} row", kind="write", state=state, reason=f"why {state}", keyword="Limits", line=20 + i)
        for i, state in enumerate(NON_OK_STATES)
    ]
    hostile = node("authored", evil, keyword="Info", line=40)
    root = node("root", "main.mwrite", kind="write", children=ok_nodes + bad_nodes + [hostile])
    note = Diagnostic("indent_jump", 2, 9, "line 9 jumps from indent 0 to 3")
    result = result_for(root, [note])

    tree = make_tree()
    tree.show_result(result)

    assert [tree.headerItem().text(i) for i in range(2)] == ["Instruction / file", "State"]
    pairs = list(zip(iter_nodes(result), (item for item, _depth in items_with_depth(tree))))
    assert len(pairs) == result.nodes + 1
    seen_state_colors = {}
    for tree_node, item in pairs:
        assert item.text(1) == badge_text(tree_node)
        assert item.data(0, ROLE_DATA) == tree_node.role
        assert item.data(0, STATE_DATA) == tree_node.state and item.data(0, KIND_DATA) == tree_node.kind
        seen_state_colors.setdefault(tree_node.state, set()).add(rgb(item.foreground(1).color()))

    by_label = {item.text(0).lstrip("+~ "): item for _node, item in pairs}
    # every kind: the State text is the kind label (empty for text/content/marker rows)
    assert by_label["operator row"].text(1) == "operator (chain ends)"
    assert by_label["write row"].text(1) == "Write" and by_label["store row"].text(1) == "Store"
    assert by_label["doc row"].text(1) == "Doc (raw text)" and by_label["prog row"].text(1) == "Prog (raw text)"
    assert by_label["folder row"].text(1) == "[folder]"
    assert all(by_label[f"{kind} row"].text(1) == "" for kind in ("text", "content", "marker"))
    # every non-ok state: `state: reason`, and a colour of its own
    for state in NON_OK_STATES:
        assert by_label[f"{state} row"].text(1) == f"{state}: why {state}"
    ok_rgb = rgb(write_tree.STATE_COLORS["ok"])
    assert seen_state_colors["ok"] == {ok_rgb, rgb(write_tree.LEAF_COLOR)}  # healthy chain ends are muted
    assert rgb(by_label["operator row"].foreground(1).color()) == rgb(write_tree.LEAF_COLOR)
    assert rgb(by_label["folder row"].foreground(1).color()) == rgb(write_tree.LEAF_COLOR)
    assert rgb(by_label["write row"].foreground(1).color()) == ok_rgb
    palette = {rgb(color) for color in write_tree.STATE_COLORS.values()}
    assert len(palette) == len(write_tree.STATE_COLORS) == 7, "every state has its own colour"
    assert rgb(write_tree.LEAF_COLOR) not in palette
    for state in ("ok", "broken", "blocked", "unsaved"):  # the editor and the tree agree
        assert rgb(write_tree.STATE_COLORS[state]) == rgb(REF_STYLES[state][0])
    for state in NON_OK_STATES:
        assert rgb(by_label[f"{state} row"].foreground(1).color()) == rgb(write_tree.STATE_COLORS[state])

    # authored vs resolved: marked below a reference, none on the root Write's own lines
    assert by_label["Goals: typed under the reference"].text(0) == "+ Goals: typed under the reference"
    assert by_label["Goals: typed under the reference"].data(0, ROLE_DATA) == "authored"
    assert by_label["Info: from the target"].text(0) == "~ Info: from the target"
    assert by_label["Info: from the target"].data(0, ROLE_DATA) == "resolved"
    assert by_label["Info: from the target"].font(0).italic()
    assert by_label["write row"].text(0) == "write row" and tree.topLevelItem(0).font(0).bold()

    # labels from someone else's file are scrubbed for display (bidi override, control char)
    shown = [item.text(0) for _node, item in pairs if item.text(0).startswith("Info: a")][0]
    assert shown == "Info: a?b?c" and chr(0x202E) not in shown
    # a grammar diagnostic of the root Write is a tooltip on the line it is attached to
    tipped = [item.text(0) for _node, item in pairs if "Note: line 9 jumps from indent 0 to 3" in item.toolTip(0)]
    assert tipped == ["text row"]  # the node on line 2


def test_the_pane_is_read_only_and_a_deep_chain_is_built_without_recursion():
    depth = 2000
    result = chain(depth)
    assert result.nodes == depth
    tree = make_tree()

    old_limit = sys.getrecursionlimit()
    sys.setrecursionlimit(len(inspect.stack(0)) + 45)
    try:
        tree.show_result(result)
    finally:
        sys.setrecursionlimit(old_limit)

    assert tree.topLevelItemCount() == 1
    seen = 0
    deepest = 0
    for item, level in items_with_depth(tree):
        seen += 1
        deepest = max(deepest, level)
        assert item.flags() == ITEM_FLAGS, "no editable, checkable, draggable or droppable rows"
    assert seen == depth + 1 and deepest == depth
    # partial expansion (2000 nodes): only depth 0..2 is open, deeper rows are not
    expanded = {level for item, level in items_with_depth(tree) if item.isExpanded()}
    assert expanded == {0, 1, 2}

    assert tree.editTriggers() == QAbstractItemView.EditTrigger.NoEditTriggers
    assert tree.dragDropMode() == QAbstractItemView.DragDropMode.NoDragDrop
    assert tree.selectionMode() == QAbstractItemView.SelectionMode.SingleSelection
    first = tree.topLevelItem(0)
    tree.editItem(first, 0)
    tree.editItem(first, 1)
    assert tree.state() != QAbstractItemView.State.EditingState
    assert tree.selectionModel() is not None
    tree.setCurrentItem(first)  # selecting is allowed
    assert tree.currentItem() is not None and tree.currentItem().text(0) == "main.mwrite"


def test_expansion_policy_and_show_result_replaces_the_previous_contents():
    tree = make_tree()

    # 5 chain nodes + 295 leaves = exactly 300 nodes: the limit itself expands everything
    small = chain(5, leaves=295)
    assert small.nodes == write_tree.EXPAND_ALL_MAX_NODES == 300
    tree.show_result(small)
    parents = [(item, level) for item, level in items_with_depth(tree) if item.childCount()]
    assert [level for _item, level in parents] == [0, 1, 2, 3, 4, 5]
    assert all(item.isExpanded() for item, _level in parents)

    # one node more: only depth 0..2 (the root Write is depth 0) stays open
    big = chain(5, leaves=296)
    assert big.nodes == 301
    tree.show_result(big)
    parents = [(item, level) for item, level in items_with_depth(tree) if item.childCount()]
    assert [(level, item.isExpanded()) for item, level in parents] == [
        (0, True), (1, True), (2, True), (3, False), (4, False), (5, False),
    ]

    # the next result REPLACES everything: no leftover rows, expansion re-decided from scratch
    tiny = result_for(node("root", "other.mwrite", kind="write", children=[node("authored", "Info: x", keyword="Info", line=1)]))
    tree.show_result(tiny)
    rows = [(item.text(0), level) for item, level in items_with_depth(tree)]
    assert rows == [("other.mwrite", 0), ("Info: x", 1)]
    assert tree.topLevelItemCount() == 1 and tree.topLevelItem(0).isExpanded()
    tree.show_result(small)  # bigger again after a big one: back to "expand all"
    assert all(item.isExpanded() for item, _level in items_with_depth(tree) if item.childCount())
    assert sum(1 for _ in items_with_depth(tree)) == 301

    # the scroll position survives a rebuild (a long, fully expanded list in a short pane)
    wide = result_for(
        node("root", "main.mwrite", kind="write", children=[node("authored", f"Info: {i}", keyword="Info", line=i + 1) for i in range(250)])
    )
    tree.show()
    tree.show_result(wide)
    bar = tree.verticalScrollBar()
    assert bar.maximum() > 40, "precondition: the list is taller than the pane"
    bar.setValue(40)
    tree.show_result(wide)
    assert bar.value() == 40
