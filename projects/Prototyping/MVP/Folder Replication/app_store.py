"""Folder Replication - a folder/file store with a live keyword-search "sieve" pane.

QMainWindow entry point: a live keyword `QLineEdit` above two independently
scrolling `rows.py` row-stack panes. The top pane only appears once the
keyword bar is non-empty and shows a pruned, green-highlighted copy of the
tree (only folders/files that lead to a keyword match survive - see
`store_model.sieve()`); the bottom pane always shows the real, full,
unfiltered tree and IS the "current directory" source of truth (there is no
separate noise-only pane - the bottom pane's full view already is that
view). A toolbar offers "New Folder" and "Add File(s)..." (`.docx`/`.txt`),
which insert into whatever folder the bottom pane currently has open (or the
real root if nothing is open - see `_current_directory` below). Right-click
a row in the bottom pane for "Delete". A status bar surfaces confirmations
and errors.

No rename, no move-between-folders, no persistence (session-only, in
memory) - see README.md's Out of scope section.
"""

import sys
from pathlib import Path

from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QInputDialog,
    QLineEdit,
    QMainWindow,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

import store_model
from rows import RowStack
from store_model import StoreFolder

_STORE_MVP_ROOT = Path(__file__).resolve().parent
SAMPLE_DIR = _STORE_MVP_ROOT.parent / "sample"


class MainWindow(QMainWindow):
    """The Folder Replication window: keyword bar, top (pruned/search) pane, bottom
    (real/full) pane, toolbar, status bar."""

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Go - Folder Replication: Folder/File Store")
        self.resize(1000, 700)

        self.root = store_model.new_root()

        # Current-directory / insertion-target rule (a specific, documented
        # judgment call - not spec-mandated): tracked from the BOTTOM pane's
        # row-stack instance only, via its open_folder_changed signal (the
        # deepest currently open StoreFolder there, or None meaning root).
        # The top/pruned pane is a transient search-result view for browsing
        # to a match, not a navigation surface for choosing where new items
        # land - its open-state is ignored entirely for this purpose. Both
        # toolbar actions below target whatever this attribute currently
        # holds.
        self._current_directory: StoreFolder = self.root

        self._build_central_widget()
        self._build_toolbar()
        self.statusBar().showMessage("Ready.")

    # -- construction -----------------------------------------------------

    def _build_central_widget(self):
        self.keyword_edit = QLineEdit()
        self.keyword_edit.setPlaceholderText("Search by name or content...")
        self.keyword_edit.textChanged.connect(self._on_keyword_changed)

        # Top pane: pruned/highlighted search-result view. Hidden whenever
        # the keyword bar is empty/whitespace-only. "Delete" is disabled
        # here - see rows.RowStack's enable_delete docstring for why.
        self.top_rows = RowStack(store_model.new_root(), highlight_folders=True, enable_delete=False)
        self.top_scroll = QScrollArea()
        self.top_scroll.setWidgetResizable(True)
        self.top_scroll.setWidget(self.top_rows)
        self.top_scroll.setVisible(False)

        # Bottom pane: the real, full, unfiltered tree - always present,
        # and the sole source of truth for "current directory".
        self.bottom_rows = RowStack(self.root, highlight_folders=False, enable_delete=True)
        self.bottom_rows.open_folder_changed.connect(self._on_current_directory_changed)
        self.bottom_scroll = QScrollArea()
        self.bottom_scroll.setWidgetResizable(True)
        self.bottom_scroll.setWidget(self.bottom_rows)

        central = QWidget()
        layout = QVBoxLayout(central)
        layout.addWidget(self.keyword_edit)
        layout.addWidget(self.top_scroll)
        layout.addWidget(self.bottom_scroll)
        self.setCentralWidget(central)

    def _build_toolbar(self):
        toolbar = self.addToolBar("Store")

        new_folder_action = toolbar.addAction("New Folder")
        new_folder_action.triggered.connect(self.new_folder)

        add_files_action = toolbar.addAction("Add File(s)...")
        add_files_action.triggered.connect(self.add_files)

    # -- keyword search ------------------------------------------------

    def _on_keyword_changed(self, keyword: str):
        """Live-search: fires on every keystroke, including a single letter."""
        if not keyword.strip():
            self.top_scroll.setVisible(False)
            return
        pruned = store_model.sieve(self.root, keyword)
        self.top_rows.set_root(pruned, reset_open_path=True)
        self.top_scroll.setVisible(True)

    # -- current-directory tracking -------------------------------------

    def _on_current_directory_changed(self, folder):
        self._current_directory = folder if folder is not None else self.root

    # -- toolbar actions -------------------------------------------------

    def new_folder(self):
        name, ok = QInputDialog.getText(self, "New Folder", "Folder name:")
        if not ok or not name.strip():
            return
        try:
            store_model.add_folder(self._current_directory, name.strip())
        except Exception as exc:  # noqa: BLE001 - surface to the status bar, don't crash the event loop
            self._report_error(f"Could not create folder: {exc}")
            return
        self._rebuild_panes()
        self.statusBar().showMessage(f"Created folder: {name.strip()}")

    def add_files(self):
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Add file(s)", str(SAMPLE_DIR), "Documents (*.docx *.txt)"
        )
        if not paths:
            return
        added = 0
        for path in paths:
            try:
                store_model.add_file(self._current_directory, path)
            except Exception as exc:  # noqa: BLE001 - surface to the status bar, keep going
                self._report_error(f"Could not add {path}: {exc}")
                continue
            added += 1
        self._rebuild_panes()
        if added:
            self.statusBar().showMessage(f"Added {added} file(s).")

    def _rebuild_panes(self):
        """Rebuild the bottom pane (always), and the top pane too if a
        keyword is currently active (the new item could itself now be a
        match, or could invalidate/change an existing match's ancestry)."""
        self.bottom_rows.refresh()
        keyword = self.keyword_edit.text()
        if keyword.strip():
            pruned = store_model.sieve(self.root, keyword)
            self.top_rows.set_root(pruned, reset_open_path=True)

    def _report_error(self, message: str) -> None:
        """Surface `message` on the status bar, prefixed with "Error: "
        (mirrors Joint Interactor/app_canvas.py's `_report_error` pattern)."""
        self.statusBar().showMessage(f"Error: {message}")


def main():
    """Launch the Folder Replication application: create the QApplication, show the
    main window, and run the Qt event loop until exit."""
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
