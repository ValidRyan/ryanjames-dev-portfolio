"""Operator nodes for the Joint Interactor free-placement canvas.

`OperatorNode` is a `QGraphicsObject` (rather than a plain `QGraphicsItem`)
so it can participate in Qt's signal/slot system. `DocxOperatorNode` and
`TxtOperatorNode` are the two concrete node kinds; each knows how to read
its own source file (`read_source()`) and how to write its own
non-destructive export (`export_path` attribute / `write_export()` method -
see `export_paths.py` and `docx_write.py`).
"""

import sys
from pathlib import Path

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QBrush, QColor, QFont, QPen
from PySide6.QtWidgets import (
    QFileDialog,
    QGraphicsObject,
    QGraphicsProxyWidget,
    QHBoxLayout,
    QLabel,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

# docx_extract.py lives under MVP/File Recompiler/ - reached the same way
# Prototyping/MVP/tests/test_docx_extract.py already does, since Program
# MVP is deliberately not turned into an installable package.
_DOC_MVP_ROOT = Path(__file__).resolve().parent
_PROGRAM_MVP_ROOT = _DOC_MVP_ROOT.parent / "File Recompiler"
if str(_PROGRAM_MVP_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROGRAM_MVP_ROOT))

import docx_extract  # noqa: E402 - import must follow the sys.path setup above

import export_paths  # noqa: E402

NODE_WIDTH = 160
NODE_HEIGHT = 80
PANEL_WIDTH = 320
PANEL_HEIGHT = 220
PANEL_GAP = 8


class OperatorNode(QGraphicsObject):
    """Base class for a canvas operator node bound to one source file.

    Holds `source_path`, `kind` ("docx" | "txt", set by subclasses),
    an `export_path` (defaulted at construction, freely overridable via
    `export_as()`), a `content` text buffer (populated via
    `load_content()`), and the list of `wires` currently attached to it.
    Moving the node updates every attached wire's path so wires stay glued
    to it as it's dragged.
    """

    kind = "operator"  # overridden by subclasses

    def __init__(self, source_path: str, parent=None):
        super().__init__(parent)
        self.source_path = str(source_path)
        self.export_path = str(export_paths.derive_default_export_path(self.source_path))
        self.content: str = ""
        self.wires: list = []
        self.on_error = None  # optional callable(str), set by the owning app window
        self._dirty = False
        self._content_panel = None
        self._content_editor = None

        self.setFlag(QGraphicsObject.GraphicsItemFlag.ItemIsMovable, True)
        self.setFlag(QGraphicsObject.GraphicsItemFlag.ItemIsSelectable, True)
        self.setFlag(QGraphicsObject.GraphicsItemFlag.ItemSendsGeometryChanges, True)
        self.setAcceptHoverEvents(True)

        self.load_content()

    # -- Qt required overrides ----------------------------------------

    def boundingRect(self) -> QRectF:
        """Return this node's fixed on-canvas rectangle (NODE_WIDTH x NODE_HEIGHT)."""
        return QRectF(0, 0, NODE_WIDTH, NODE_HEIGHT)

    def paint(self, painter, option, widget=None):
        """Draw the node's rounded-rect body (filled per `_fill_color()`) and
        a "<KIND>\\n<filename>" label."""
        rect = self.boundingRect()
        painter.setBrush(QBrush(self._fill_color()))
        painter.setPen(QPen(QColor("black"), 1.5))
        painter.drawRoundedRect(rect, 8, 8)

        painter.setPen(QPen(QColor("black")))
        painter.setFont(QFont("Segoe UI", 9))
        label = f"{self.kind.upper()}\n{Path(self.source_path).name}"
        painter.drawText(
            rect.adjusted(6, 6, -6, -6),
            int(Qt.AlignmentFlag.AlignLeft | Qt.TextFlag.TextWordWrap),
            label,
        )

    def _fill_color(self) -> QColor:
        """Body fill color for `paint()`; overridden per-subclass (default: neutral gray)."""
        return QColor("#e5e7eb")  # overridden per-subclass

    def itemChange(self, change, value):
        """After a move, refresh every attached wire's path so wires stay
        glued to this node as it's dragged."""
        if change == QGraphicsObject.GraphicsItemChange.ItemPositionHasChanged:
            for wire in list(self.wires):
                wire.update_path()
        return super().itemChange(change, value)

    def contextMenuEvent(self, event):
        """Show the right-click "Run" / "Export" / "Export As..." /
        "Display Contents" (or "Hide Contents") / "Reload from Source" /
        "Delete" menu and dispatch the chosen action."""
        menu = QMenu()
        run_action = menu.addAction("Run")
        export_action = menu.addAction("Export")
        export_as_action = menu.addAction("Export As...")
        panel_open = self._content_panel is not None and self._content_panel.isVisible()
        display_label = "Hide Contents" if panel_open else "Display Contents"
        display_action = menu.addAction(display_label)
        reload_action = menu.addAction("Reload from Source")
        menu.addSeparator()
        delete_action = menu.addAction("Delete")
        chosen = menu.exec(event.screenPos())
        if chosen is run_action:
            self.run()
        elif chosen is export_action:
            self.export()
        elif chosen is export_as_action:
            self.export_as()
        elif chosen is display_action:
            self.toggle_display_contents()
        elif chosen is reload_action:
            self.reload_from_source()
        elif chosen is delete_action:
            self.delete()

    # -- node behavior --------------------------------------------------

    def run(self):
        """Designate this node as signal source; fire every attached wire.

        Each attached wire independently resolves its own FlowPlan (via
        flow.resolve()) against its own toggle state - Run merely attempts
        the flow, it does not guarantee anything actually happens (e.g. an
        Off wire attempted this way stays a no-op dead-wire). A failure to
        reload this node's own content (e.g. its source file was moved or
        deleted since it was added) is reported via `on_error` instead of
        raising into the Qt context-menu event handler.
        """
        try:
            self.load_content()
        except Exception as exc:  # noqa: BLE001 - surface to the status bar, don't crash the event loop
            self._report_error(f"Run failed: could not reload {self.source_path}: {exc}")
            return
        for wire in list(self.wires):
            wire.trigger_flow(self)

    def delete(self):
        """Detach every attached wire, then remove this node from the scene."""
        for wire in list(self.wires):
            wire.detach()
        scene = self.scene()
        if scene is not None:
            scene.removeItem(self)

    def _report_error(self, message: str) -> None:
        """Surface `message` via `on_error` if the owning window set one.

        Falls back to stderr so a failure is never silently swallowed even
        when this node isn't wired up to a status bar (e.g. constructed
        directly, outside of app_canvas.py).
        """
        if self.on_error is not None:
            self.on_error(message)
        else:
            print(f"[operators] {message}", file=sys.stderr)

    def load_content(self, force: bool = False) -> str:
        """Refresh `content` from disk via `read_source()`, unless a pending
        edit is in progress (`self._dirty`) and `force` is not set - protects
        an in-progress user edit from being silently discarded by an
        unrelated reload (e.g. flow.execute()'s own `from_node.load_content()`
        call, or a future Run on this same node). Clears `_dirty` whenever a
        real reload actually happens, since freshly-read content is by
        definition no longer "pending edits not yet reflected on disk".

        This is the single choke point for panel refresh: whenever a real
        reload actually happens (i.e. the dirty-guard above doesn't
        short-circuit), it also calls `_refresh_displayed_content()` so the
        open Display Contents panel (if any) is kept in sync. This closes a
        previously-flagged gap where a node's own panel could go stale right
        after its own `Run` - `run()` calls `load_content()` too, so that
        path now refreshes the panel for free rather than only doing so via
        `reload_from_source()`."""
        if self._dirty and not force:
            return self.content
        self.content = self.read_source()
        self._dirty = False
        self._refresh_displayed_content()
        return self.content

    def toggle_display_contents(self):
        """Lazily build the content panel on first use, then flip its
        visibility. Refreshes the displayed text whenever the panel becomes
        visible so it never shows stale text from before it was last hidden."""
        if self._content_panel is None:
            self._build_content_panel()
        now_visible = not self._content_panel.isVisible()
        self._content_panel.setVisible(now_visible)
        if now_visible:
            self._refresh_displayed_content()

    def _build_content_panel(self):
        """Construct the embedded QGraphicsProxyWidget content panel: a small
        header row (label + Reload + Hide buttons) above a QPlainTextEdit,
        parented to this node via setParentItem so it automatically tracks
        the node's position on drag with no manual repositioning needed."""
        container = QWidget()
        container.setFixedSize(PANEL_WIDTH, PANEL_HEIGHT)
        layout = QVBoxLayout(container)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(2)

        header = QHBoxLayout()
        header.addWidget(QLabel("Content"))
        header.addStretch(1)
        reload_button = QPushButton("Reload")
        reload_button.clicked.connect(self.reload_from_source)
        header.addWidget(reload_button)
        hide_button = QPushButton("Hide")
        hide_button.clicked.connect(self.toggle_display_contents)
        header.addWidget(hide_button)
        layout.addLayout(header)

        editor = QPlainTextEdit()
        editor.setPlainText(self.content)
        editor.textChanged.connect(self._on_widget_text_changed)
        layout.addWidget(editor)

        self._content_editor = editor

        proxy = QGraphicsProxyWidget()
        proxy.setWidget(container)
        proxy.setParentItem(self)
        proxy.setPos(0, NODE_HEIGHT + PANEL_GAP)
        proxy.setVisible(False)

        self._content_panel = proxy

    def _on_widget_text_changed(self):
        """Connected to the embedded editor's textChanged signal: pushes the
        user's live edits into `content` and marks the node dirty. Also
        notifies every attached wire of the live edit (`Wire.notify_content_edit`)
        so a connected node's panel can mirror the change in real time,
        synchronously, with no disk round-trip."""
        self.content = self._content_editor.toPlainText()
        self._dirty = True
        for wire in list(self.wires):
            wire.notify_content_edit(self)

    def _refresh_displayed_content(self):
        """Push `self.content` into the editor, but only if the panel is
        currently open and the text actually differs - guarded with
        blockSignals() so this programmatic update doesn't itself trigger
        `_on_widget_text_changed` (which would wrongly mark the node dirty
        and create a feedback loop), and skipped when equal to avoid
        needless cursor-jump/flicker on every unedited Run."""
        if self._content_panel is None or not self._content_panel.isVisible():
            return
        if self._content_editor.toPlainText() == self.content:
            return
        self._content_editor.blockSignals(True)
        self._content_editor.setPlainText(self.content)
        self._content_editor.blockSignals(False)

    def reload_from_source(self):
        """Context-menu action / header 'Reload' button: always force-reloads
        from disk, discarding any pending edit, and clears dirty. Panel
        refresh is handled internally by `load_content()` now (it's the
        single choke point for that), so no explicit refresh call is needed
        here anymore."""
        try:
            self.load_content(force=True)
        except Exception as exc:  # noqa: BLE001 - surface to the status bar, don't crash the event loop
            self._report_error(f"Reload failed: could not reload {self.source_path}: {exc}")
            return

    def receive_content(self, text: str) -> None:
        """Hook called by flow.execute() when this node is a flow target. If
        this node is currently dirty (an in-progress edit pending), skip
        updating content/the display - protects the edit from being clobbered
        by a wire firing mid-edit. The export file still gets written as
        normal by the caller either way; this only guards the in-memory
        buffer and its on-screen display."""
        if self._dirty:
            return
        self.content = text
        self._refresh_displayed_content()

    def export(self):
        """No-dialog re-save to the existing export_path (context menu
        'Export'), for re-saving without reopening the Save-As dialog."""
        return self.write_export(self.content)

    # -- subclass hooks (must override) ----------------------------------

    def read_source(self) -> str:
        """Read and return this node's content from its `source_path`. Must be overridden."""
        raise NotImplementedError

    def write_export(self, text: str) -> str:
        """Write `text` out to this node's export_path and return it as a string."""
        export_paths.assert_not_original(self.export_path, known_originals(self))
        export_paths.write_formatted(text, self.export_path)
        return self.export_path

    def export_as(self):
        """Open a native Save-As dialog to override this node's export_path.

        Accepts any name/extension/location. Validated inline at confirm
        time: if the chosen path collides with any operator's real original
        source, warn and re-open the dialog rather than silently accepting
        it or only surfacing the failure later at write-time. Returns the
        newly chosen path, or None if the user cancelled.
        """
        while True:
            chosen, _ = QFileDialog.getSaveFileName(None, "Export As...", self.export_path)
            if not chosen:
                return None
            try:
                export_paths.assert_not_original(chosen, known_originals(self))
            except ValueError:
                QMessageBox.warning(
                    None,
                    "Invalid export target",
                    "That path matches an operator's original source file. "
                    "Choose a different export target.",
                )
                continue
            self.export_path = chosen
            self.write_export(self.content)
            return chosen


class DocxOperatorNode(OperatorNode):
    """A canvas node bound to a real .docx source file."""

    kind = "docx"

    def read_source(self) -> str:
        """Extract and return the plain text of this node's .docx source file."""
        return docx_extract.extract_text(self.source_path)

    def _fill_color(self) -> QColor:
        """Light blue fill, marking this node as a Docx operator."""
        return QColor("#dbeafe")  # light blue


class TxtOperatorNode(OperatorNode):
    """A canvas node bound to a real .txt source file."""

    kind = "txt"

    def read_source(self) -> str:
        """Read and return this node's .txt source file as plain text."""
        return Path(self.source_path).read_text(encoding="utf-8")

    def _fill_color(self) -> QColor:
        """Light green fill, marking this node as a Txt operator."""
        return QColor("#dcfce7")  # light green


def known_originals(node: "OperatorNode") -> list:
    """Collect every operator's original `source_path` currently on the canvas.

    Used as the `known_originals` guard set passed to
    `export_paths.assert_not_original()`. Falls back to just this node's
    own `source_path` if it isn't attached to a scene yet (e.g. a bare node
    under test, with no QGraphicsScene involved).
    """
    scene = node.scene()
    if scene is None:
        return [node.source_path]

    originals = []
    for item in scene.items():
        if isinstance(item, OperatorNode):
            originals.append(item.source_path)
    return originals
