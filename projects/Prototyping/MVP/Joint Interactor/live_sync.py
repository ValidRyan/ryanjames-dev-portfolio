"""LiveSync: one QFileSystemWatcher + a debounced QTimer, owned 1:1 by a Wire.

Armed against a designated source node's `source_path` only when that
wire's resolved `FlowPlan.watch` flag is True; disarmed otherwise. Callers
(Wire) are expected to call `rearm(path_or_None)` after every
`trigger_flow()` - so toggling Off, or a cadence that resolves to ONCE,
tears the watcher down on the very next resolve; toggling On (with a source
already designated via Run) arms it; Running the *other* endpoint re-arms
against the new source.

Windows note: files are commonly saved via delete+recreate, which can
silently drop the watched path from QFileSystemWatcher's internal list -
`_on_file_changed` defensively re-adds it so subsequent saves keep firing.
"""

from PySide6.QtCore import QFileSystemWatcher, QObject, QTimer, Signal

DEBOUNCE_MS = 250


class LiveSync(QObject):
    """Owns a single watched path (or none) plus a debounce timer.

    `on_fire` is an optional plain callable (not a Qt slot requirement)
    invoked once per debounced change, in addition to the `fired` signal -
    this keeps it easy to wire up from a plain Wire method without needing
    Wire itself to be a QObject subclass beyond what QGraphicsPathItem
    already provides.
    """

    fired = Signal()

    def __init__(self, on_fire=None, parent=None):
        super().__init__(parent)
        self._on_fire = on_fire
        self._watched_path: str | None = None

        self._watcher = QFileSystemWatcher(self)
        self._watcher.fileChanged.connect(self._on_file_changed)

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(DEBOUNCE_MS)
        self._timer.timeout.connect(self._on_debounced)

    @property
    def watched_path(self) -> str | None:
        return self._watched_path

    def rearm(self, path: str | None) -> None:
        """(Re)arm the watcher against `path`; disarm entirely if `path` is None.

        Always tears down any prior watch first, so this is safe to call
        unconditionally after every `trigger_flow()` regardless of whether
        the source or toggle state actually changed.
        """
        self.disarm()
        if path is None:
            return
        self._watched_path = path
        self._watcher.addPath(path)

    def disarm(self) -> None:
        """Tear down any active watch and stop the pending debounce timer."""
        watched_files = self._watcher.files()
        if watched_files:
            self._watcher.removePaths(watched_files)
        self._timer.stop()
        self._watched_path = None

    def _on_file_changed(self, path: str) -> None:
        """Handle QFileSystemWatcher's fileChanged signal: re-add the watched
        path if Windows' save-via-delete+recreate dropped it, then (re)start
        the debounce timer."""
        if self._watched_path is None:
            return  # stray/queued signal after disarm() - do not resurrect the timer

        # Windows commonly saves via delete+recreate, which drops `path`
        # from the watcher's internal file list even though we still
        # consider ourselves armed against it - re-add it defensively so
        # the *next* save still fires too.
        if self._watched_path not in self._watcher.files():
            self._watcher.addPath(self._watched_path)
        self._timer.start()  # (re)start the debounce window, coalescing bursts

    def _on_debounced(self) -> None:
        """Fire once the debounce window elapses: emit `fired` and invoke `on_fire`."""
        self.fired.emit()
        if self._on_fire is not None:
            self._on_fire()
