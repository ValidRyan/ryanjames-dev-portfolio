"""SAP Modifier - the window: a thin shell that wires the finished pieces together.

`MainWindow(QMainWindow)` puts a `WriteEditor` on the left and, on the right, the
`TreePane` (the resolved tree) over a read-only Run log. It owns only what no
piece underneath owns: the root file's path (`self._path`, None = unsaved), the
toolbar / shortcuts / dialogs and the refresh schedule. Everything else is a call:

    write_editor   the seven tools, `to_model` / `outline` / `load_model`, signals
    write_tree     `TreePane.show_result(result)`
    write_resolve  `resolve_tree(lines, root_file=...)` and `run_tree(result)`
    write_format   `read_write_file` / `save_write` (the only code that writes a file)
    write_paths    `ext_for` (dialog filters, Save As suffix), `real_root`, `relative_ref_for`

Refresh (outline -> resolve -> tree + counts) runs debounced 250 ms after the
editor reports a REAL edit (`content_edited`, never a highlighter pass) and at once
on Save, window activation and the Refresh action. `self.last_result` keeps the
latest `ResolveResult` (the tests read it). File dialogs and message boxes are
reached through the module-level `QFileDialog` / `QMessageBox` names so tests can
patch them; nothing here runs, opens or follows a referenced file (the resolver
does the reading, and only under its path policy).

Documented choices (the plan leaves them open):
- New and Open ask the same Save / Discard / Cancel question as closing does when
  the Write is modified, so they cannot silently throw edits away.
- The toolbar Italics button runs the full cycle (`cycle_italic`); Ctrl+I is a
  separate window-level action for `toggle_italic_right` (decision D5). Outdent is
  its own action rather than Shift-click on Indent.
- Each Run replaces the log (it shows the latest run, matching the tree beside it).
- Insert reference is refused BEFORE its dialog opens when the Write is unsaved.
- Save As opens in the current file's folder, else the home folder (never `sample/`).

Explicitly out of scope: undo/redo buttons (the editor has Ctrl+Z / Ctrl+Y),
recent files, autosave, and running any instruction (they are duds).
"""

import os
import sys
from pathlib import Path

from PySide6.QtCore import QEvent, Qt, QTimer
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QSplitter,
)

from write_editor import WriteEditor
from write_format import WriteFormatError, read_write_file, save_write
from write_grammar import LineCounts
from write_model import SPREAD_STEP
from write_paths import ext_for, real_root, relative_ref_for
from write_resolve import resolve_tree, run_tree
from write_tree import TreePane

_WRITE_MVP_ROOT = Path(__file__).resolve().parent
SAMPLE_DIR = _WRITE_MVP_ROOT.parent / "sample"

# Same debounce as the Joint Interactor's live refresh (`Joint Interactor/live_sync.py:18`): long enough
# to coalesce typing, short enough to feel live.
DEBOUNCE_MS = 250
MAX_LOG_BLOCKS = 5000


def _write_filter() -> str:
    """The file-dialog filter for a Write, built from the extension registry."""
    return f"Write files (*{ext_for('write')});;All files (*)"


class MainWindow(QMainWindow):
    """The SAP Modifier window: editor | (tree over Run log), toolbar, status bar."""

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Go - SAP Modifier: Script Processor")
        self.resize(1000, 700)

        self._path: str | None = None  # the root Write on disk; None = unsaved
        self.last_result = None  # the latest ResolveResult
        self.actions_by_name: dict[str, QAction] = {}

        # Debounced refresh: every real edit restarts the single-shot timer.
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(DEBOUNCE_MS)
        self._timer.timeout.connect(self.refresh)

        self._build_central_widget()
        self._build_toolbar()
        self.status_label = QLabel(self)
        self.statusBar().addPermanentWidget(self.status_label)
        self._wire_editor()

        self.editor.setFocus()
        self.refresh()
        self._sync_tools()
        self.statusBar().showMessage("Ready.")

    # -- construction -----------------------------------------------------

    def _build_central_widget(self):
        self.editor = WriteEditor(self)
        self.tree = TreePane(self)
        self.run_log = QPlainTextEdit(self)
        self.run_log.setReadOnly(True)
        self.run_log.setMaximumBlockCount(MAX_LOG_BLOCKS)
        self.run_log.setPlaceholderText("Run log: press Run (F5).")

        right = QSplitter(Qt.Orientation.Vertical, self)
        right.addWidget(self.tree)
        right.addWidget(self.run_log)
        right.setSizes([420, 220])
        main = QSplitter(Qt.Orientation.Horizontal, self)
        main.addWidget(self.editor)
        main.addWidget(right)
        main.setSizes([480, 520])  # the tree needs room: labels are long and the State column is 190 px
        self.setCentralWidget(main)

    def _add_action(self, bar, name, slot, shortcut=None, checkable=False) -> QAction:
        """One toolbar QAction wired with `triggered` (never `toggled`: no feedback loops)."""
        action = QAction(name, self)
        action.setCheckable(checkable)
        if shortcut:
            action.setShortcut(QKeySequence(shortcut))
        action.triggered.connect(slot)
        bar.addAction(action)
        self.actions_by_name[name] = action
        return action

    def _build_toolbar(self):
        bar = self.addToolBar("Write")
        ed = self.editor
        add = self._add_action
        add(bar, "New", self.new_write, "Ctrl+N")
        add(bar, "Open", self.open_write, "Ctrl+O")
        add(bar, "Save", self.save, "Ctrl+S")
        add(bar, "Save As", self.save_as, "Ctrl+Shift+S")
        bar.addSeparator()
        for name, slot, shortcut in (("Bold", ed.toggle_bold, "Ctrl+B"), ("Line", ed.toggle_line, None), ("Dash", ed.toggle_dash, None)):
            action = add(bar, name, slot, shortcut, checkable=True)
            # Runs after the editor's slot: if the tool refused, Qt's own check-box flip is put right.
            action.triggered.connect(self._sync_tools)
        add(bar, "Italics", ed.cycle_italic)
        add(bar, "Spread", self._spread_wider)
        add(bar, "Narrow", self._spread_narrower)
        add(bar, "Embed", ed.embed_selection)
        add(bar, "Un-embed", ed.unembed_selection)
        add(bar, "Indent", self._indent)
        add(bar, "Outdent", self._outdent)
        bar.addSeparator()
        add(bar, "Insert reference...", self._insert_reference)
        add(bar, "Insert Store reference...", self._insert_store_reference)
        add(bar, "Run", self.run, "F5")
        add(bar, "Refresh", self.refresh)
        # Ctrl+I only leans right (D5); the toolbar Italics button runs the whole cycle.
        italic_right = QAction("Italic right", self)
        italic_right.setShortcut(QKeySequence("Ctrl+I"))
        italic_right.triggered.connect(ed.toggle_italic_right)
        self.addAction(italic_right)
        self.actions_by_name["Italic right"] = italic_right

    def _wire_editor(self):
        ed = self.editor
        ed.content_edited.connect(self._on_content_edited)
        ed.tool_state_changed.connect(self._on_tool_state)
        ed.status_message.connect(self.statusBar().showMessage)
        ed.error_message.connect(self._report_error)
        ed.document().modificationChanged.connect(self._update_status)

    # -- status -----------------------------------------------------------

    def _report_error(self, message: str) -> None:
        """Surface `message` on the status bar with an "Error: " prefix (as the other MVPs do)."""
        self.statusBar().showMessage(f"Error: {message}")

    def _update_status(self, *_args) -> None:
        counts = self.last_result.counts if self.last_result is not None else LineCounts()
        state = "Modified" if self.editor.document().isModified() else "Saved"
        self.status_label.setText(
            f"Clarity {counts.clarity} | Noise {counts.noise} | Comments {counts.comment} | {state}"
        )

    def _on_tool_state(self, state: dict) -> None:
        """Show the editor's tool state on the checkable actions, with their signals blocked."""
        for key, name in (("bold", "Bold"), ("line", "Line"), ("dash", "Dash")):
            action = self.actions_by_name[name]
            action.blockSignals(True)
            action.setChecked(bool(state.get(key)))
            action.blockSignals(False)

    def _sync_tools(self) -> None:
        self._on_tool_state(self.editor.current_tool_state())

    # -- refresh and run --------------------------------------------------

    def _on_content_edited(self) -> None:
        self._timer.start()  # restarts the interval: the refresh fires once typing pauses

    def refresh(self) -> None:
        """Outline -> resolve -> tree pane + counts; runs on the UI thread (the resolver's valves bound it)."""
        self._timer.stop()
        result = resolve_tree(self.editor.outline(), root_file=self._path)
        self.tree.show_result(result)
        self.last_result = result
        self._update_status()

    def run(self) -> None:
        """Refresh synchronously, then log one line per instruction node (they are all duds)."""
        self.refresh()
        lines = run_tree(self.last_result)
        self.run_log.setPlainText("\n".join(lines) if lines else "No instruction lines to run.")
        self.statusBar().showMessage(f"Ran {len(lines)} instruction(s).")

    def changeEvent(self, event) -> None:
        """Activation (coming back from another program or a dialog): files may have changed on disk."""
        super().changeEvent(event)
        if event.type() == QEvent.Type.ActivationChange and self.isActiveWindow():
            self.editor.highlighter.invalidate()
            self.refresh()

    # -- file actions -----------------------------------------------------

    def _confirm_discard(self) -> bool:
        """True when it is fine to replace / close the Write: clean, saved, or discarded on request."""
        if not self.editor.document().isModified():
            return True
        buttons = QMessageBox.StandardButton
        answer = QMessageBox.question(
            self,
            "Unsaved changes",
            "This Write has unsaved changes. Save them first?",
            buttons.Save | buttons.Discard | buttons.Cancel,
            buttons.Save,
        )
        if answer == buttons.Save:
            return self.save()  # a failed save keeps the window (and the edits)
        return answer == buttons.Discard

    def new_write(self) -> None:
        if not self._confirm_discard():
            return
        self.editor.clear_all()
        self._path = None
        self.editor.set_root_file(None)
        self.editor.document().setModified(False)
        self.refresh()
        self.statusBar().showMessage("New Write.")

    def open_write(self) -> None:
        if not self._confirm_discard():
            return
        start = os.path.dirname(self._path) if self._path else str(SAMPLE_DIR)
        path, _ = QFileDialog.getOpenFileName(self, "Open Write", start, _write_filter())
        if path:
            self.load_path(path)

    def load_path(self, path: str) -> bool:
        """Read `path` first; only a good read replaces the editor, the path and the modified flag."""
        try:
            loaded = read_write_file(path)
        except (WriteFormatError, OSError) as exc:
            self._report_error(f"Could not open {path}: {exc}")
            return False
        warnings = list(loaded.warnings) + list(self.editor.load_model(loaded.document))
        self._path = path
        self.editor.set_root_file(path)
        self.editor.document().setModified(False)
        self.refresh()
        note = f" ({len(warnings)} warning(s))" if warnings else ""
        self.statusBar().showMessage(f"Opened {path}{note}")
        return True

    def save(self) -> bool:
        return self._save_to(self._path) if self._path else self.save_as()

    def save_as(self) -> bool:
        start = os.path.dirname(self._path) if self._path else str(Path.home())
        path, _ = QFileDialog.getSaveFileName(self, "Save Write As", start, _write_filter())
        if not path:
            return False
        suffix = ext_for("write")
        if not path.lower().endswith(suffix):
            path += suffix
        return self._save_to(path)

    def _save_to(self, path: str) -> bool:
        """Atomic save (write_format); on any error report it and stay modified."""
        try:
            save_write(path, self.editor.to_model())
        except (WriteFormatError, OSError) as exc:
            self._report_error(f"Could not save {path}: {exc}")
            return False
        self._path = path
        self.editor.set_root_file(path)  # re-classifies every reference against this folder
        self.editor.document().setModified(False)
        self.refresh()
        self.statusBar().showMessage(f"Saved {path}")
        return True

    def closeEvent(self, event) -> None:
        if self._confirm_discard():
            event.accept()
        else:
            event.ignore()

    # -- tools that need an argument --------------------------------------

    def _spread_wider(self) -> None:
        self.editor.spread_by(SPREAD_STEP)

    def _spread_narrower(self) -> None:
        self.editor.spread_by(-SPREAD_STEP)

    def _indent(self) -> None:
        self.editor.indent_selection(1)

    def _outdent(self) -> None:
        self.editor.indent_selection(-1)

    # -- references -------------------------------------------------------

    def _reference_root(self) -> str | None:
        """The link-free folder references are relative to, or None (after saying why) when unsaved."""
        if self._path is None:
            self._report_error("Save the Write first: a reference is relative to the folder the Write is saved in.")
            return None
        return real_root(os.path.dirname(os.path.realpath(self._path)))

    def _insert_reference(self) -> None:
        root_real = self._reference_root()
        if root_real is None:
            return
        path, _ = QFileDialog.getOpenFileName(self, "Insert reference", root_real, "All files (*)")
        if path:
            self._insert_relative(path, root_real)

    def _insert_store_reference(self) -> None:
        root_real = self._reference_root()
        if root_real is None:
            return
        path = QFileDialog.getExistingDirectory(self, "Insert Store reference", root_real)
        if path:
            self._insert_relative(path, root_real)

    def _insert_relative(self, target: str, root_real: str) -> None:
        """Insert `@relative/path` for `target`, or report why the path policy refuses it."""
        rel, reason = relative_ref_for(real_root(target), root_real, root_real)
        if rel is None:
            self._report_error(f"Cannot insert that reference: {reason}.")
        else:
            self.editor.insert_reference(rel)


def main():
    """Launch the SAP Modifier: create the QApplication, show the window, run the event loop."""
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
