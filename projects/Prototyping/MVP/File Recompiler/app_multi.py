"""PySide6 launcher for the multi-file a(instruction)b(instruction)c prototype.

Extends the ternary prototype (app_ternary.py) to several .docx files at
once: each chosen file gets its own row in a vertically-scrolling list, with
its own editable midput text (the row system "extends downward" as more
files are chosen, per spec). A -> B and B -> C instructions are each a single
global dropdown, reused from instructions.py, applied uniformly to every row.

Export prompts for a choice: combine every row into one .txt, or write one
sibling .txt per source file (same convention as the first two prototypes).

No live sync, no filesystem watching, no folder/filetype mechanics - same
standing exclusions as the other prototypes.
"""

import sys
from pathlib import Path

from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

import docx_extract
from instructions import INSTRUCTIONS
from multi_export import build_combined_text


class FileRow(QWidget):
    """One row: a chosen file's name plus its own editable midput text."""

    def __init__(self, source_path: str, initial_text: str):
        super().__init__()
        self.source_path = source_path

        layout = QVBoxLayout()
        layout.addWidget(QLabel(Path(source_path).name))

        self.text_edit = QTextEdit()
        self.text_edit.setPlainText(initial_text)
        self.text_edit.setFixedHeight(120)
        layout.addWidget(self.text_edit)

        self.setLayout(layout)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Go - Multi-File Prototype")
        self.resize(750, 600)

        self.rows: list[FileRow] = []

        self.choose_button = QPushButton("Choose .docx files...")
        self.choose_button.clicked.connect(self.choose_docx_files)

        self.ab_instruction_combo = QComboBox()
        self.ab_instruction_combo.addItems(list(INSTRUCTIONS.keys()))
        self.ab_instruction_combo.setCurrentText("Equivalate (==)")

        self.bc_instruction_combo = QComboBox()
        self.bc_instruction_combo.addItems(list(INSTRUCTIONS.keys()))
        self.bc_instruction_combo.setCurrentText("Calculate (=)")

        self.export_button = QPushButton("Export...")
        self.export_button.clicked.connect(self.export_rows)
        self.export_button.setEnabled(False)

        self.status_label = QLabel("No files chosen yet.")

        self.rows_container = QWidget()
        self.rows_layout = QVBoxLayout()
        self.rows_layout.addStretch(1)  # keeps rows top-packed as more are added
        self.rows_container.setLayout(self.rows_layout)

        self.scroll_area = QScrollArea()
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setWidget(self.rows_container)

        top_row = QHBoxLayout()
        top_row.addWidget(self.choose_button)
        top_row.addWidget(QLabel("A -> B instruction:"))
        top_row.addWidget(self.ab_instruction_combo)

        bottom_row = QHBoxLayout()
        bottom_row.addWidget(QLabel("B -> C instruction:"))
        bottom_row.addWidget(self.bc_instruction_combo)
        bottom_row.addWidget(self.export_button)

        layout = QVBoxLayout()
        layout.addLayout(top_row)
        layout.addWidget(self.scroll_area)
        layout.addLayout(bottom_row)
        layout.addWidget(self.status_label)

        container = QWidget()
        container.setLayout(layout)
        self.setCentralWidget(container)

    def _add_row(self, source_path: str, text: str):
        row = FileRow(source_path, text)
        self.rows_layout.insertWidget(self.rows_layout.count() - 1, row)
        self.rows.append(row)

    def choose_docx_files(self):
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Choose .docx files", "", "Word Documents (*.docx)"
        )
        if not paths:
            return

        instruction_fn = INSTRUCTIONS[self.ab_instruction_combo.currentText()]
        already_loaded = {str(Path(row.source_path).resolve()) for row in self.rows}
        loaded = 0
        for path in paths:
            resolved = str(Path(path).resolve())
            if resolved in already_loaded:
                self.status_label.setText(f"Skipped {path}: already loaded (would overwrite its sibling .txt).")
                continue
            already_loaded.add(resolved)

            try:
                text = docx_extract.extract_text(path)
            except Exception as exc:  # noqa: BLE001 - surface any read error, keep going
                self.status_label.setText(f"Failed to read {path}: {exc}")
                continue

            try:
                result, message = instruction_fn(text)
            except Exception as exc:  # noqa: BLE001 - surface any instruction error, keep going
                self.status_label.setText(f"A -> B instruction failed on {path}: {exc}")
                continue

            if result is None:
                self.status_label.setText(message)
                continue

            self._add_row(path, result)
            loaded += 1

        if loaded:
            self.export_button.setEnabled(True)
            self.status_label.setText(f"Loaded {loaded} file(s).")

    def export_rows(self):
        if not self.rows:
            self.status_label.setText("Choose at least one .docx file first.")
            return

        box = QMessageBox(self)
        box.setWindowTitle("Export")
        box.setText("Export all rows into one combined file, or one file per row?")
        one_file_button = box.addButton("One combined file", QMessageBox.ButtonRole.AcceptRole)
        separate_button = box.addButton("Separate files", QMessageBox.ButtonRole.AcceptRole)
        box.addButton(QMessageBox.StandardButton.Cancel)
        box.exec()

        clicked = box.clickedButton()
        if clicked is one_file_button:
            self._export_combined()
        elif clicked is separate_button:
            self._export_separate()
        # else: cancelled - do nothing

    def _apply_bc_to_all_rows(self):
        """Run the current B -> C instruction over every row.

        Returns a list of (source_path, result_text) on full success, or
        None if any row failed/returned "not implemented" (status label is
        already set with the reason in that case).
        """
        instruction_fn = INSTRUCTIONS[self.bc_instruction_combo.currentText()]
        named_results = []
        for row in self.rows:
            try:
                result, message = instruction_fn(row.text_edit.toPlainText())
            except Exception as exc:  # noqa: BLE001 - surface any instruction error
                self.status_label.setText(f"B -> C instruction failed on {row.source_path}: {exc}")
                return None

            if result is None:
                self.status_label.setText(message)
                return None

            named_results.append((row.source_path, result))
        return named_results

    def _export_combined(self):
        named_results = self._apply_bc_to_all_rows()
        if named_results is None:
            return

        save_path, _ = QFileDialog.getSaveFileName(
            self, "Save combined .txt", "", "Text Files (*.txt)"
        )
        if not save_path:
            return

        combined_text = build_combined_text(named_results)
        try:
            Path(save_path).write_text(combined_text, encoding="utf-8")
        except Exception as exc:  # noqa: BLE001 - surface any write error
            self.status_label.setText(f"Failed to export: {exc}")
            return

        self.status_label.setText(f"Exported combined file to: {save_path}")

    def _export_separate(self):
        named_results = self._apply_bc_to_all_rows()
        if named_results is None:
            return

        exported = []
        for source_path, result in named_results:
            txt_path = Path(source_path).with_suffix(".txt")
            try:
                txt_path.write_text(result, encoding="utf-8")
            except Exception as exc:  # noqa: BLE001 - surface any write error, stop here
                if exported:
                    self.status_label.setText(
                        f"Failed to export {txt_path}: {exc} "
                        f"({len(exported)} file(s) already written before the failure: "
                        f"{', '.join(exported)})"
                    )
                else:
                    self.status_label.setText(f"Failed to export {txt_path}: {exc}")
                return
            exported.append(str(txt_path))

        self.status_label.setText(f"Exported {len(exported)} file(s).")


if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())
