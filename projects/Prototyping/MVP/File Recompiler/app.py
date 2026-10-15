"""Minimal PySide6 launcher for the "equivalate" prototype.

Opens a single plain window with:
  - a "Choose .docx..." button that extracts and previews text
  - an "Export to .txt" button that writes that text verbatim to disk

No live sync, no filesystem watching, no folder/filetype mechanics -
this is a one-shot, button-driven pipeline only.
"""

import sys
from pathlib import Path

from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QLabel,
    QMainWindow,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

import docx_extract


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Go - Equivalate Prototype")
        self.resize(600, 500)

        self.docx_path: str | None = None

        self.choose_button = QPushButton("Choose .docx...")
        self.choose_button.clicked.connect(self.choose_docx)

        self.export_button = QPushButton("Export to .txt")
        self.export_button.clicked.connect(self.export_txt)
        self.export_button.setEnabled(False)

        self.text_preview = QTextEdit()
        self.text_preview.setReadOnly(True)

        self.status_label = QLabel("No file chosen yet.")

        layout = QVBoxLayout()
        layout.addWidget(self.choose_button)
        layout.addWidget(self.text_preview)
        layout.addWidget(self.export_button)
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

        self.docx_path = path
        self.text_preview.setPlainText(text)
        self.export_button.setEnabled(True)
        self.status_label.setText(f"Loaded: {path}")

    def export_txt(self):
        if not self.docx_path:
            self.status_label.setText("Choose a .docx file first.")
            return

        txt_path = Path(self.docx_path).with_suffix(".txt")
        try:
            txt_path.write_text(self.text_preview.toPlainText(), encoding="utf-8")
        except Exception as exc:  # noqa: BLE001 - surface any write error to the user
            self.status_label.setText(f"Failed to export: {exc}")
            return

        self.status_label.setText(f"Exported to: {txt_path}")


if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())
