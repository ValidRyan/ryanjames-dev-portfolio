"""Minimal PySide6 launcher for the ternary a(instruction)b(instruction)c prototype.

Opens a single plain window with:
  - a "Choose .docx..." button (input, a) paired with an A -> B instruction
    dropdown, defaulting to Equivalate
  - an editable text box representing midput (b), the app surface itself -
    the user may hand-edit this before exporting
  - a B -> C instruction dropdown, defaulting to Calculate, paired with an
    "Export to .txt" button (output, c)

No live sync, no filesystem watching, no folder/filetype mechanics - each
hop fires once, on demand, exactly like the first (binary) prototype.
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
    QPushButton,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

import docx_extract
from instructions import INSTRUCTIONS


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Go - Ternary Prototype")
        self.resize(700, 550)

        self.docx_path: str | None = None

        self.choose_button = QPushButton("Choose .docx...")
        self.choose_button.clicked.connect(self.choose_docx)

        self.ab_instruction_combo = QComboBox()
        self.ab_instruction_combo.addItems(list(INSTRUCTIONS.keys()))
        self.ab_instruction_combo.setCurrentText("Equivalate (==)")

        self.bc_instruction_combo = QComboBox()
        self.bc_instruction_combo.addItems(list(INSTRUCTIONS.keys()))
        self.bc_instruction_combo.setCurrentText("Calculate (=)")

        self.export_button = QPushButton("Export to .txt")
        self.export_button.clicked.connect(self.export_txt)
        self.export_button.setEnabled(False)

        self.midput_edit = QTextEdit()  # editable - the app surface itself

        self.status_label = QLabel("No file chosen yet.")

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
        layout.addWidget(QLabel("Midput (b) - the app surface, editable"))
        layout.addWidget(self.midput_edit)
        layout.addLayout(bottom_row)
        layout.addWidget(self.status_label)

        container = QWidget()
        container.setLayout(layout)
        self.setCentralWidget(container)

    def choose_docx(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Choose .docx file", "", "Word Documents (*.docx)"
        )
        if not path:
            return

        try:
            text = docx_extract.extract_text(path)
        except Exception as exc:  # noqa: BLE001 - surface any read error to the user
            self.status_label.setText(f"Failed to read {path}: {exc}")
            return

        instruction_fn = INSTRUCTIONS[self.ab_instruction_combo.currentText()]
        try:
            result, message = instruction_fn(text)
        except Exception as exc:  # noqa: BLE001 - surface any instruction error to the user
            self.status_label.setText(f"A -> B instruction failed: {exc}")
            return

        if result is None:
            self.status_label.setText(message)
            return

        self.docx_path = path
        self.midput_edit.setPlainText(result)
        self.export_button.setEnabled(True)
        self.status_label.setText(f"Loaded: {path}")

    def export_txt(self):
        if not self.docx_path:
            self.status_label.setText("Choose a .docx file first.")
            return

        instruction_fn = INSTRUCTIONS[self.bc_instruction_combo.currentText()]
        try:
            result, message = instruction_fn(self.midput_edit.toPlainText())
        except Exception as exc:  # noqa: BLE001 - surface any instruction error to the user
            self.status_label.setText(f"B -> C instruction failed: {exc}")
            return

        if result is None:
            self.status_label.setText(message)
            return

        txt_path = Path(self.docx_path).with_suffix(".txt")
        try:
            txt_path.write_text(result, encoding="utf-8")
        except Exception as exc:  # noqa: BLE001 - surface any write error to the user
            self.status_label.setText(f"Failed to export: {exc}")
            return

        self.status_label.setText(f"Exported to: {txt_path}")


if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())
