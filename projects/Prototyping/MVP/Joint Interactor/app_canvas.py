"""Joint Interactor - Free-Placement Operator Canvas.

QMainWindow entry point: a QGraphicsScene/QGraphicsView canvas onto which
Docx and Txt operator nodes can be freely placed and freely wired (real
chaining/fan-out/fan-in, not capped to a single pair like File Recompiler). A
toolbar offers "Add Docx Operator", "Add Txt Operator", and a checkable
"Draw Wire" mode (click a source node, then click a target node - no
drag-handles). A status bar surfaces errors and confirmations.

Right-click a node for "Run" (designate as signal source, fire every
attached wire) / "Delete". Right-click a wire to toggle its 5 independent
On/Off/Uni/Bi/One-shot options.
"""

import sys
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QPainter
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QGraphicsScene,
    QGraphicsView,
    QMainWindow,
)

from operators import DocxOperatorNode, OperatorNode, TxtOperatorNode
from wires import Wire

_DOC_MVP_ROOT = Path(__file__).resolve().parent
SAMPLE_DIR = _DOC_MVP_ROOT.parent / "sample"

_SPAWN_STEP = 40
_SPAWN_COLS = 6


class CanvasView(QGraphicsView):
    """QGraphicsView that intercepts left-clicks while "Draw Wire" mode is on."""

    def __init__(self, scene: QGraphicsScene, main_window: "MainWindow"):
        super().__init__(scene)
        self.main_window = main_window
        self.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.setDragMode(QGraphicsView.DragMode.RubberBandDrag)

    def mousePressEvent(self, event):
        """While "Draw Wire" mode is checked, route left-clicks to the
        main window's wire-click handling instead of the default click/
        rubber-band-select behavior."""
        if self.main_window.draw_wire_action.isChecked() and event.button() == Qt.MouseButton.LeftButton:
            item = self.itemAt(event.pos())
            node = self._resolve_node(item)
            if node is not None:
                self.main_window.handle_wire_click(node)
            else:
                # Clicked empty canvas (or a wire, which isn't a valid wire
                # endpoint) while in Draw Wire mode - give feedback instead
                # of silently doing nothing, and don't fall through to
                # rubber-band selection, which would be a confusing mix of
                # two interaction modes at once.
                self.main_window.handle_wire_click_miss()
            return
        super().mousePressEvent(event)

    @staticmethod
    def _resolve_node(item):
        """Walk up from a clicked scene item to its owning OperatorNode, if any."""
        while item is not None and not isinstance(item, OperatorNode):
            item = item.parentItem()
        return item


class MainWindow(QMainWindow):
    """The free-placement operator canvas window: toolbar, scene/view, and
    all node/wire creation and status-bar reporting logic."""

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Go - Joint Interactor: Free-Placement Operator Canvas")
        self.resize(1000, 700)

        self._pending_wire_source = None
        self._spawn_count = 0

        self.scene = QGraphicsScene(self)
        self.scene.setSceneRect(0, 0, 4000, 4000)
        self.view = CanvasView(self.scene, self)
        self.setCentralWidget(self.view)

        self._build_toolbar()
        self.statusBar().showMessage("Ready.")

    def _build_toolbar(self):
        """Build the "Operators" toolbar: Add Docx/Txt actions and the
        checkable Draw Wire action."""
        toolbar = self.addToolBar("Operators")

        add_docx_action = toolbar.addAction("Add Docx Operator")
        add_docx_action.triggered.connect(self.add_docx_operator)

        add_txt_action = toolbar.addAction("Add Txt Operator")
        add_txt_action.triggered.connect(self.add_txt_operator)

        toolbar.addSeparator()

        self.draw_wire_action = toolbar.addAction("Draw Wire")
        self.draw_wire_action.setCheckable(True)
        self.draw_wire_action.toggled.connect(self._on_draw_wire_toggled)

    # -- node creation -----------------------------------------------------

    def add_docx_operator(self):
        """Prompt for a .docx file and add it to the canvas as a DocxOperatorNode."""
        path, _ = QFileDialog.getOpenFileName(
            self, "Choose .docx file", str(SAMPLE_DIR), "Word Documents (*.docx)"
        )
        if not path:
            return
        self._add_node(DocxOperatorNode, path)

    def add_txt_operator(self):
        """Prompt for a .txt file and add it to the canvas as a TxtOperatorNode."""
        path, _ = QFileDialog.getOpenFileName(
            self, "Choose .txt file", str(SAMPLE_DIR), "Text Files (*.txt)"
        )
        if not path:
            return
        self._add_node(TxtOperatorNode, path)

    def _add_node(self, node_cls, path: str):
        """Construct `node_cls(path)`, wire up error reporting, and place it
        on the scene at the next spawn position; reports load failures to
        the status bar instead of raising."""
        try:
            node = node_cls(path)
        except Exception as exc:  # noqa: BLE001 - surface any load error to the user
            self.statusBar().showMessage(f"Failed to load {path}: {exc}")
            return

        node.on_error = self._report_error
        node.setPos(*self._next_spawn_pos())
        self.scene.addItem(node)
        self.statusBar().showMessage(f"Added {node.kind} operator: {path}")

    def _next_spawn_pos(self):
        """Return the next (x, y) grid slot for a newly added node, advancing
        the spawn counter so nodes don't stack on top of each other."""
        col = self._spawn_count % _SPAWN_COLS
        row = self._spawn_count // _SPAWN_COLS
        self._spawn_count += 1
        return (40 + col * (160 + _SPAWN_STEP), 40 + row * (80 + _SPAWN_STEP))

    # -- wire drawing mode ---------------------------------------------------

    def _on_draw_wire_toggled(self, checked: bool):
        """Reset any pending wire source and update the status bar whenever
        Draw Wire mode is turned on or off."""
        self._pending_wire_source = None
        if checked:
            self.statusBar().showMessage("Draw Wire mode: click a source node, then a target node.")
        else:
            self.statusBar().showMessage("Ready.")

    def handle_wire_click(self, node):
        """Advance Draw Wire mode's source/target state machine: the first
        click designates `node` as the pending source, the second creates a
        Wire between it and `node` (with self-wire and stale-source guards)."""
        if self._pending_wire_source is not None and self._pending_wire_source.scene() is None:
            # The previously-selected source node was deleted (right-click ->
            # Delete) while still pending - drop the stale reference instead
            # of wiring a phantom node into a new Wire.
            self._pending_wire_source = None
            self.statusBar().showMessage("Wire source was deleted - selection reset, click a source node again.")
            return

        if self._pending_wire_source is None:
            self._pending_wire_source = node
            self.statusBar().showMessage(
                f"Wire source: {Path(node.source_path).name} - now click a target node."
            )
            return

        if node is self._pending_wire_source:
            self.statusBar().showMessage("Cannot wire a node to itself - click a different target node.")
            return

        wire = Wire(self._pending_wire_source, node)
        wire.on_error = self._report_error
        self.scene.addItem(wire)
        self.statusBar().showMessage(
            f"Wired {Path(self._pending_wire_source.source_path).name} <-> {Path(node.source_path).name}"
        )
        self._pending_wire_source = None

    def handle_wire_click_miss(self):
        """Report via the status bar that a Draw Wire mode click missed every
        node, without clearing any already-pending wire source."""
        if self._pending_wire_source is not None:
            self.statusBar().showMessage(
                "Draw Wire: that click didn't land on a node - the pending source is still "
                "selected, click a node to finish the wire."
            )
        else:
            self.statusBar().showMessage("Draw Wire mode: click a source node, then a target node.")

    def _report_error(self, message: str) -> None:
        """Surface `message` on the status bar, prefixed with "Error: "."""
        self.statusBar().showMessage(f"Error: {message}")


def main():
    """Launch the Joint Interactor application: create the QApplication, show the
    main window, and run the Qt event loop until exit."""
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
