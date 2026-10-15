"""The open-upward row-stack widget for Folder Replication's folder/file browser.

`RowStack` renders ONE subtree (either the real full tree for the bottom
pane, or a pruned/green copy for the top pane) as a stack of horizontal
rows, using an "open-upward" interaction model - think of it as a small
stack, not a tree of simultaneously-open branches:

- `_open_path: list[StoreFolder]` is the currently-drilled-down chain from
  the root: `_open_path[0]` is a folder open at the root level,
  `_open_path[1]` is a folder open inside `_open_path[0]`, etc. Empty list =
  nothing drilled into, only the root row shows.
- The ROOT level's children render in a row at the BOTTOM of the widget
  (left-to-right, arrival order). Each successive open folder's children
  render in a row ABOVE the row that contains the folder that was clicked to
  open it, so the deepest open folder's children end up TOP-most and root is
  always BOTTOM-most.
- Clicking a folder `F` in the row rendering depth `d`'s contents:
  collapses (`_open_path = _open_path[:d]`) if `F` was already open at that
  row; otherwise replaces (`_open_path = _open_path[:d] + [F]`), which
  naturally discards any deeper opens stacked above that row. Clicking a
  file never touches `_open_path`.
- Rows are fully rebuilt from `_open_path` + the subtree root on every
  change - simplest correct approach for a prototype this size, no
  diffing/patching of the QVBoxLayout.
"""

import weakref

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QHBoxLayout, QMenu, QPushButton, QVBoxLayout, QWidget

from store_model import StoreFile, StoreFolder, delete_item

# Reserved specifically for the pruned/search pane's folder-match highlight -
# per spec, don't reuse this for anything else (e.g. TxtOperatorNode's own
# light-green fill in Joint Interactor/operators.py is a different shade, on purpose).
_HIGHLIGHT_COLOR = "#86efac"
_NEUTRAL_FOLDER_COLOR = "#e5e7eb"  # gray - matches operators.py's default neutral fill
_FILE_COLOR = "#f8fafc"  # near-white, neutral


class _EntryButton(QPushButton):
    """One row entry (a folder or a file). Plain QPushButton, plus a
    right-click "Delete" context menu (mirrors `operators.py`'s
    `contextMenuEvent` pattern) - this is a plain QWidget context menu
    event, NOT a QGraphicsItem one, so `event.globalPos()` is the correct
    call here, not `event.screenPos()`."""

    def __init__(self, label: str, on_delete, parent=None):
        super().__init__(label, parent)
        self.setFlat(True)
        self._on_delete = on_delete

    def contextMenuEvent(self, event):
        if self._on_delete is None:
            return
        menu = QMenu(self)
        delete_action = menu.addAction("Delete")
        chosen = menu.exec(event.globalPos())
        if chosen is delete_action:
            self._on_delete()


class RowStack(QWidget):
    """Renders one folder subtree as an open-upward stack of rows.

    Parameterized by:
      - `root`: the subtree to render (the real tree for the bottom pane, or
        a pruned copy from `store_model.sieve()` for the top pane).
      - `highlight_folders`: when True, every folder renders with a green
        background (the pruned/search pane); when False, neutral styling.
      - `enable_delete`: whether right-clicking a row offers "Delete".
        Judgment call, documented here: the top/pruned pane renders a
        COPY of the real tree's folders (per `store_model.sieve()`'s own
        docstring), so a "Delete" there would only remove a row from that
        transient copy, not from the real store - it would silently
        reappear on the next keystroke's re-search, which is worse than not
        offering it. `app_store.py` therefore constructs the top pane's
        instance with `enable_delete=False`; only the bottom (real, full
        tree) pane offers Delete.

    `open_folder_changed` fires with the deepest currently open
    `StoreFolder`, or `None` if `_open_path` is empty (nothing drilled into,
    root is "current").
    """

    open_folder_changed = Signal(object)

    def __init__(self, root: StoreFolder, highlight_folders: bool = False, enable_delete: bool = True, parent=None):
        super().__init__(parent)
        self._root = root
        self._highlight_folders = highlight_folders
        self._enable_delete = enable_delete
        self._open_path: list = []

        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(4, 4, 4, 4)
        self._layout.setSpacing(4)

        self._rebuild()

    # -- public API ----------------------------------------------------

    def set_root(self, root: StoreFolder, reset_open_path: bool = True) -> None:
        """Swap in a new subtree root and rebuild.

        `reset_open_path` defaults to True: a pruned tree's shape can change
        completely on every keyword keystroke, so any stale `_open_path`
        referencing folders that may no longer exist in the new pruned copy
        would be a bug. The bottom (full-tree) pane in `app_store.py` never
        calls `set_root()` at all (it is constructed once against the real
        root, which never changes identity); this method exists purely for
        the pruned/top pane's per-keystroke rebuild.
        """
        self._root = root
        if reset_open_path:
            self._open_path = []
        self._rebuild()

    def refresh(self) -> None:
        """Rebuild rows against the CURRENT root/_open_path - call this after
        the underlying tree changed elsewhere (e.g. a toolbar add), so this
        instance picks up the change without losing its current drill-down
        state."""
        self._drop_open_path_entries_no_longer_present()
        self._rebuild()

    def current_open_folder(self) -> StoreFolder | None:
        """Return the deepest currently open folder, or None (meaning the
        real root is "current")."""
        return self._open_path[-1] if self._open_path else None

    # -- open-path bookkeeping ------------------------------------------

    def _drop_open_path_entries_no_longer_present(self) -> None:
        """Defensive guard for `refresh()`: truncate `_open_path` at the
        first folder that is no longer actually a child of the previous
        link (e.g. it, or an ancestor, was deleted elsewhere), so the next
        rebuild never tries to render a deleted folder's children."""
        valid_path = []
        current_children = self._root.children
        for folder in self._open_path:
            if any(child is folder for child in current_children):
                valid_path.append(folder)
                current_children = folder.children
            else:
                break
        self._open_path = valid_path

    def _handle_folder_clicked(self, folder: StoreFolder, depth: int) -> None:
        if depth < len(self._open_path) and self._open_path[depth] is folder:
            self._open_path = self._open_path[:depth]  # collapse
        else:
            self._open_path = self._open_path[:depth] + [folder]  # replace
        self._rebuild()
        self.open_folder_changed.emit(self.current_open_folder())

    def _handle_delete(self, parent: StoreFolder, item) -> None:
        delete_item(parent, item)
        if isinstance(item, StoreFolder):
            for index, folder in enumerate(self._open_path):
                if folder is item:
                    self._open_path = self._open_path[:index]
                    break
        self._rebuild()
        self.open_folder_changed.emit(self.current_open_folder())

    # -- rendering -------------------------------------------------------

    def _rebuild(self) -> None:
        while self._layout.count():
            taken = self._layout.takeAt(0)
            widget = taken.widget()
            if widget is not None:
                widget.deleteLater()

        max_depth = len(self._open_path)
        # Top-to-bottom: deepest open folder's children first (topmost), ...,
        # root's children last (bottommost).
        for depth in range(max_depth, -1, -1):
            folder = self._root if depth == 0 else self._open_path[depth - 1]
            self._layout.addWidget(self._build_row(folder, depth))

    def _build_row(self, folder: StoreFolder, depth: int) -> QWidget:
        row_widget = QWidget()
        row_layout = QHBoxLayout(row_widget)
        row_layout.setContentsMargins(0, 0, 0, 0)
        row_layout.setSpacing(4)

        for item in folder.children:
            row_layout.addWidget(self._build_entry(folder, item, depth))
        row_layout.addStretch(1)
        return row_widget

    def _build_entry(self, parent: StoreFolder, item, depth: int) -> _EntryButton:
        is_folder = isinstance(item, StoreFolder)
        label = f"{item.name}/" if is_folder else item.name

        # The callbacks close over a WEAK reference to this RowStack, never over `self`. A closure over `self`
        # (the previous lambdas) makes a reference cycle through C++ (RowStack -> button -> signal connection ->
        # closure -> RowStack) that `gc` cannot see, so a parentless RowStack stays alive for the whole process.
        # Its queued `deleteLater()` rows then run in whatever test spins the event loop next, and PySide deletes
        # the owner from inside the destructor of its own child: an access violation (exit 139), reproducible
        # with only test_rows.py followed by test_live_sync.py. Behaviour is otherwise identical.
        weak_self = weakref.ref(self)

        def on_delete_cb(p=parent, i=item):
            stack = weak_self()
            if stack is not None:
                stack._handle_delete(p, i)

        on_delete = on_delete_cb if self._enable_delete else None
        button = _EntryButton(label, on_delete=on_delete)

        if is_folder and self._highlight_folders:
            button.setStyleSheet(f"background-color: {_HIGHLIGHT_COLOR};")
        elif is_folder:
            button.setStyleSheet(f"background-color: {_NEUTRAL_FOLDER_COLOR};")
        else:
            button.setStyleSheet(f"background-color: {_FILE_COLOR};")

        if is_folder:

            def on_click(checked=False, f=item, d=depth):
                stack = weak_self()
                if stack is not None:
                    stack._handle_folder_clicked(f, d)

            button.clicked.connect(on_click)

        return button
