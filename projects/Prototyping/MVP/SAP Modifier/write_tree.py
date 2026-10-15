"""The read-only resolved-tree pane of the SAP Modifier.

`TreePane(QTreeWidget)` shows a `write_resolve.ResolveResult` beside the editor:
the root Write, then every instruction line, and under each special-instruction
(SI) reference the lines authored beneath it followed by the target's own
contents resolved live. It is a VIEW only - nothing here reads a file or decides
what a reference means; it draws what `resolve_tree` produced. Imports only
`write_resolve` (for `badge_text` and the node types, by duck typing) and Qt;
`app_write.py` owns one and calls `show_result(...)` after every refresh.

Columns: "Instruction / file" (the node label) and "State" (`badge_text(node)`:
the kind label such as `Write` or `operator (chain ends)` when the node is ok,
else `state: reason`). The State text is coloured per state - the same palette the
highlighter uses in the editor (ok teal, broken red, blocked orange, unsaved
grey-blue) plus cycle, truncated and structural - and a problem row also tints
its label. An ok operator or ordinary folder (a leaf that cannot hold anything)
is a muted grey, so a healthy chain end never looks like an error.

Authored vs resolved is marked twice over: every row stores its `role` in
`ROLE_DATA` (`"root"`, `"authored"`, `"resolved"`), and below the root's own
lines the label carries an ASCII marker - `+ ` for a line AUTHORED under a
reference, `~ ` (italic, blue-grey) for content RESOLVED from the reference's
target. The root Write's own top-level lines carry no marker (they are simply the
document), and tooltips spell the meaning out.

Safety and performance:
- `show_result` is one explicit-stack loop (no recursion), so a 2000-deep chain
  cannot exhaust Python's stack (the resolver's own valves keep real trees near
  `MAX_DEPTH`, but the pane does not rely on that).
- The item tree is built DETACHED and attached with one `addTopLevelItem`, with
  `setUpdatesEnabled(False)` around the whole rebuild: no per-row model signals
  and no repaint storm for a 2000-node result. The scroll position is restored.
- Rows are read-only (`ItemIsEnabled | ItemIsSelectable`, no edit triggers, no
  drag and drop); text is plain (`setText`, never HTML - nothing here calls
  `setHtml`).
- Labels and reasons can come from someone else's file, so control, format
  (bidi overrides, zero-width) and surrogate characters are shown as `?` and long
  text is cut (`MAX_TEXT_CHARS`); the full state text stays in the tooltip.

Documented choices: expansion policy - a result of `EXPAND_ALL_MAX_NODES` (300)
nodes or fewer is expanded completely, a bigger one only through depth
`PARTIAL_EXPAND_DEPTH` (2; the root Write is depth 0), so a fan-out result opens
as a readable outline; grammar diagnostics of the ROOT Write (an indent jump, an
indented first line) are shown as a tooltip on the line they are attached to,
not as extra rows.

Explicitly out of scope: editing, double-click-to-jump, sorting, filtering, and
running anything (the Run log lives in the window).
"""

import unicodedata

from PySide6.QtCore import Qt
from PySide6.QtGui import QBrush, QColor, QFont
from PySide6.QtWidgets import QAbstractItemView, QHeaderView, QTreeWidget, QTreeWidgetItem

from write_resolve import badge_text

EXPAND_ALL_MAX_NODES = 300
PARTIAL_EXPAND_DEPTH = 2
MAX_TEXT_CHARS = 300
MAX_TOOLTIP_CHARS = 1000

HEADERS = ("Instruction / file", "State")

# Extra data roles on every item (column 0): the node's role, state and kind.
ROLE_DATA = Qt.ItemDataRole.UserRole
STATE_DATA = Qt.ItemDataRole.UserRole + 1
KIND_DATA = Qt.ItemDataRole.UserRole + 2

# One colour per state (QColor is a plain value type, safe at module level).
# The first four match `write_highlight.REF_STYLES` so the editor and the tree
# agree; the other three have no editor counterpart.
STATE_COLORS = {
    "ok": QColor(0, 128, 128),
    "broken": QColor(200, 32, 32),
    "blocked": QColor(224, 112, 0),
    "unsaved": QColor(96, 122, 160),
    "cycle": QColor(140, 40, 170),
    "truncated": QColor(160, 120, 0),
    "structural": QColor(120, 72, 24),
}
# An ok operator / ordinary folder is a healthy chain end, not a link: muted grey.
LEAF_COLOR = QColor(110, 110, 110)
LEAF_KINDS = frozenset({"operator", "folder"})
# Content resolved from a reference's target (italic label colour when the row is ok).
RESOLVED_COLOR = QColor(70, 100, 140)

_MARKERS = {"authored": "+ ", "resolved": "~ "}
_TOOLTIPS = {
    "root": "The root Write",
    "authored": "Authored: a line typed under its parent in the same Write",
    "resolved": "Resolved live from the parent's target (nothing is copied)",
}
_FLAGS = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable


def state_color(state: str, kind: str = "text") -> QColor:
    """The colour of a node's State text: by state, grey for an ok operator/folder."""
    if state == "ok" and kind in LEAF_KINDS:
        return QColor(LEAF_COLOR)
    return QColor(STATE_COLORS.get(state, LEAF_COLOR))


def _safe(text: str, limit: int = MAX_TEXT_CHARS) -> str:
    """Make text safe to show: control/format/surrogate characters -> `?`, long text cut."""
    if not isinstance(text, str):
        text = str(text)
    clean = text[: limit + 1]
    # `isprintable` runs at C speed and is False for every Cc/Cf/Cs character (and a few
    # harmless others), so the per-character scrub only runs for text that needs a look.
    if not clean.isprintable():
        clean = "".join("?" if unicodedata.category(ch) in ("Cc", "Cf", "Cs") else ch for ch in clean)
    return clean if len(clean) <= limit else clean[: limit - 3] + "..."


class TreePane(QTreeWidget):
    """Read-only tree of a `ResolveResult`; call `show_result(result)` to (re)populate it."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setColumnCount(len(HEADERS))
        self.setHeaderLabels(list(HEADERS))
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setDragDropMode(QAbstractItemView.DragDropMode.NoDragDrop)
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.setRootIsDecorated(True)
        self.setUniformRowHeights(True)
        self.setTextElideMode(Qt.TextElideMode.ElideRight)
        # The label column takes whatever room is left (long labels elide; the tooltip carries the rest) and
        # the State column keeps a modest fixed width. Sizing State to its contents was tried and is WRONG: its
        # text includes the reason ('cycle: a.mwrite is already open above it'), which would squeeze the labels.
        header = self.header()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Interactive)
        self.setColumnWidth(1, 190)

    # -- public API ---------------------------------------------------------

    def show_result(self, result) -> None:
        """Replace the pane's contents with the tree of `result` (see the module docstring)."""
        vbar = self.verticalScrollBar()
        hbar = self.horizontalScrollBar()
        scroll = (vbar.value(), hbar.value())
        expand_all = result.nodes <= EXPAND_ALL_MAX_NODES
        self.setUpdatesEnabled(False)
        try:
            self.clear()
            root_item, expandable = self._build(result)
            self.addTopLevelItem(root_item)  # C++ owns the whole subtree from here on
            for item, depth in expandable:
                if expand_all or depth <= PARTIAL_EXPAND_DEPTH:
                    item.setExpanded(True)
        finally:
            self.setUpdatesEnabled(True)
        self.doItemsLayout()  # so the scroll bar range is right before the value is put back
        vbar.setValue(scroll[0])
        hbar.setValue(scroll[1])

    # -- building -----------------------------------------------------------

    def _build(self, result) -> tuple:
        """Build the DETACHED item tree; return `(root_item, [(item, depth), ...])` for items with children.

        One explicit stack (no recursion). Each popped node creates all of its
        children's items at once, so a parent's item is complete before the
        loop moves on.
        """
        notes = {}
        for diag in getattr(result, "diagnostics", ()):
            notes.setdefault(diag.line, []).append(diag.message)
        root = result.root
        root_item = self._make_item(root, None, notes, root.trail)
        expandable: list = []
        stack = [(root, root_item, 0)]
        while stack:
            node, item, depth = stack.pop()
            if not node.children:
                continue
            expandable.append((item, depth))
            for child in node.children:
                child_item = self._make_item(child, node, notes, root.trail, item)
                stack.append((child, child_item, depth + 1))
        return root_item, expandable

    def _make_item(self, node, parent_node, notes: dict, root_trail: str, parent_item=None) -> QTreeWidgetItem:
        """One row for `node`, appended to `parent_item` (or left detached for the root)."""
        item = QTreeWidgetItem(parent_item) if parent_item is not None else QTreeWidgetItem()
        item.setFlags(_FLAGS)
        role = node.role
        # Below the root's own lines the role is worth marking: that is where a
        # reference's authored lines and its resolved contents sit side by side.
        marked = parent_node is not None and parent_node.role != "root"
        marker = _MARKERS.get(role, "") if marked else ""
        item.setText(0, marker + _safe(node.label))
        state_text = badge_text(node)
        item.setText(1, _safe(state_text))
        item.setData(0, ROLE_DATA, role)
        item.setData(0, STATE_DATA, node.state)
        item.setData(0, KIND_DATA, node.kind)

        color = state_color(node.state, node.kind)
        item.setForeground(1, QBrush(color))
        if node.state != "ok":
            item.setForeground(0, QBrush(color))
        elif role == "resolved":
            item.setForeground(0, QBrush(RESOLVED_COLOR))
        if role in ("root", "resolved"):
            label_font = QFont(self.font())  # the view's own font, not the application default
            if role == "root":
                label_font.setBold(True)
            else:
                label_font.setItalic(True)
            item.setFont(0, label_font)

        tip = [_TOOLTIPS.get(role, "")]
        if node.ref is not None:
            tip.append(f"Reference: @{node.ref}")
        if node.line:
            tip.append(f"Line {node.line}")
        if node.trail:
            tip.append(f"In: {node.trail}")
        if node.state != "ok" or state_text:
            tip.append(f"State: {state_text or node.state}")
        if node.trail == root_trail and node.line in notes and node.keyword is not None:
            tip.extend(f"Note: {message}" for message in notes[node.line])
        # Scrub each piece, THEN join with real newlines (`_safe` would turn them into "?").
        item.setToolTip(0, "\n".join(_safe(t, MAX_TOOLTIP_CHARS) for t in tip if t)[:MAX_TOOLTIP_CHARS])
        item.setToolTip(1, _safe(state_text, MAX_TOOLTIP_CHARS))
        return item
