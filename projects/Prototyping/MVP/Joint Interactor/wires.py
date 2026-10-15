"""Wire: the QGraphicsPathItem connecting two operator nodes on the canvas.

Owns a `WireState` (5 independent booleans, imported from `flow.py`, which
is where the Qt-free dataclass lives so it stays test-friendly without a
QApplication), one `LiveSync` instance, and tracks `last_source_node` -
whichever endpoint Run was most recently invoked on. Right-click gives a
checkable menu toggling each of the 5 options independently. Rendering is
driven by the wire's currently-resolved `FlowPlan.shape`: dashed grey for
dead, thin solid for pulse-based, thick solid for live, single vs. double
arrowheads for uni- vs. bi-directional flow.
"""

import sys

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QBrush, QColor, QPainterPath, QPainterPathStroker, QPen, QPolygonF
from PySide6.QtWidgets import QGraphicsPathItem, QMenu

import flow
from flow import Endpoint, WireState
from live_sync import LiveSync

_TOGGLE_FIELDS = (
    ("on", "On (continuous live sync)"),
    ("off", "Off (dead wire)"),
    ("uni", "Uni (one-way)"),
    ("bi", "Bi (both ways)"),
    ("one_shot", "One-shot (single pulse)"),
)

_ARROW_SIZE = 10.0

# QGraphicsPathItem's default shape() (used for hit-testing - selection,
# right-click) strokes the path with the item's own `pen()`, which is never
# set here (paint() builds a local QPen per-shape instead) and so stays at
# Qt's 1px default - a wire rendered up to 3.5px thick would still only be
# reliably right-clickable along a razor-thin 1px line. _HIT_TEST_WIDTH
# widens the actual clickable/selectable region independent of the
# currently-rendered pen, so nodes and wires are comparably easy to target
# via the real QGraphicsView event flow.
_HIT_TEST_WIDTH = 14.0


class Wire(QGraphicsPathItem):
    """Connects `node_a` and `node_b`; direction/behavior is resolved live.

    `node_a`/`node_b` are fixed at creation time (Endpoint.A / Endpoint.B
    respectively) purely as stable identifiers for `flow.resolve()` - actual
    flow direction is decided per-Run by whichever node `trigger_flow()` was
    called with (`last_source_node`), not by creation order.
    """

    def __init__(self, node_a, node_b, parent=None):
        super().__init__(parent)
        self.node_a = node_a
        self.node_b = node_b
        self.state = WireState()
        self.last_source_node = None
        self.live_sync = LiveSync(on_fire=self._on_watch_fire)
        self.on_error = None  # optional callable(str), set by the owning app window

        self.setZValue(-1.0)  # draw behind operator nodes
        self.setFlag(QGraphicsPathItem.GraphicsItemFlag.ItemIsSelectable, True)

        node_a.wires.append(self)
        node_b.wires.append(self)

        self.update_path()

    # -- endpoint helpers -------------------------------------------------

    def _endpoint_of(self, node) -> Endpoint:
        """Return which Endpoint (A or B) `node` corresponds to on this wire."""
        if node is self.node_a:
            return Endpoint.A
        if node is self.node_b:
            return Endpoint.B
        raise ValueError("node is not an endpoint of this wire")

    def other_node(self, node):
        """Return this wire's opposite endpoint node relative to `node`."""
        return self.node_b if node is self.node_a else self.node_a

    def _current_plan(self) -> flow.FlowPlan:
        """Resolve the FlowPlan implied by the current state, for rendering
        and for any caller that wants to inspect the wire's live behavior
        without actually triggering a flow. Direction defaults to node_a ->
        node_b (Endpoint.A) until a real source has been designated via
        Run - resolve()'s `shape` output never actually depends on
        `source_endpoint`, only `legs`/direction does.
        """
        source_endpoint = self._endpoint_of(self.last_source_node) if self.last_source_node is not None else Endpoint.A
        return flow.resolve(self.state, source_endpoint)

    # -- geometry / rendering ----------------------------------------------

    def update_path(self):
        """Recompute the straight-line path between node_a and node_b's
        current scene centers and trigger a repaint."""
        if self.node_a is None or self.node_b is None:
            return
        a_center = self.node_a.sceneBoundingRect().center()
        b_center = self.node_b.sceneBoundingRect().center()
        path = QPainterPath(a_center)
        path.lineTo(b_center)
        self.setPath(path)
        self.update()

    def boundingRect(self):
        """Return the path's bounding rect, padded so arrowheads aren't clipped."""
        return self.path().boundingRect().adjusted(-_ARROW_SIZE, -_ARROW_SIZE, _ARROW_SIZE, _ARROW_SIZE)

    def shape(self):
        """Widen the clickable/selectable region beyond the 1px default.

        See the `_HIT_TEST_WIDTH` note above - without this, right-clicking
        a wire to reach its toggle menu is only reliable if the cursor lands
        on the exact mathematical line between the two node centers.
        """
        stroker = QPainterPathStroker()
        stroker.setWidth(_HIT_TEST_WIDTH)
        return stroker.createStroke(self.path())

    def paint(self, painter, option, widget=None):
        """Draw the wire's line (styled per the currently-resolved
        FlowPlan.shape) plus its directional arrowhead(s)."""
        plan = self._current_plan()
        pen = self._pen_for_shape(plan.shape)
        painter.setPen(pen)
        painter.drawPath(self.path())
        self._draw_arrowheads(painter, plan, pen.color())

    def _pen_for_shape(self, shape: str) -> QPen:
        """Return the QPen (color/style/width) for a given FlowPlan shape:
        dashed grey for dead, thin black for pulse-based, thick blue for live."""
        if shape == "dead-wire":
            pen = QPen(QColor("gray"))
            pen.setStyle(Qt.PenStyle.DashLine)
            pen.setWidthF(1.5)
        elif shape in ("pulse-wire", "mirror-pulse-wire"):
            pen = QPen(QColor("black"))
            pen.setStyle(Qt.PenStyle.SolidLine)
            pen.setWidthF(1.5)
        else:  # "live-wire", "live-mirror-wire"
            pen = QPen(QColor("#2563eb"))
            pen.setStyle(Qt.PenStyle.SolidLine)
            pen.setWidthF(3.5)
        return pen

    def _draw_arrowheads(self, painter, plan: flow.FlowPlan, color: QColor):
        """Draw one arrowhead per leg in `plan.legs`, pointing from source to
        target endpoint (two arrowheads, one per direction, for a mirrored plan)."""
        if not plan.legs:
            return  # dead-wire: no direction to indicate

        centers = {
            Endpoint.A: self.node_a.sceneBoundingRect().center(),
            Endpoint.B: self.node_b.sceneBoundingRect().center(),
        }
        painter.setBrush(QBrush(color))
        for from_endpoint, to_endpoint in plan.legs:
            start = centers[from_endpoint]
            end = centers[to_endpoint]
            self._draw_arrowhead_at(painter, end, end - start)

    def _draw_arrowhead_at(self, painter, tip: QPointF, direction: QPointF):
        """Draw a small filled triangle at `tip`, pointing along `direction`."""
        length = (direction.x() ** 2 + direction.y() ** 2) ** 0.5
        if length == 0:
            return
        ux, uy = direction.x() / length, direction.y() / length
        back = QPointF(tip.x() - ux * _ARROW_SIZE, tip.y() - uy * _ARROW_SIZE)
        perp = QPointF(-uy, ux) * (_ARROW_SIZE * 0.4)
        triangle = QPolygonF([tip, back + perp, back - perp])
        painter.drawPolygon(triangle)

    # -- context menu: 5 independent toggles + delete ----------------------

    def contextMenuEvent(self, event):
        """Show the right-click menu of 5 independent checkable option toggles
        plus "Delete Wire", and apply whichever action was chosen."""
        menu = QMenu()
        action_to_field = {}
        for field_name, label in _TOGGLE_FIELDS:
            action = menu.addAction(label)
            action.setCheckable(True)
            action.setChecked(getattr(self.state, field_name))
            action_to_field[action] = field_name

        menu.addSeparator()
        delete_action = menu.addAction("Delete Wire")

        chosen = menu.exec(event.screenPos())
        if chosen is None:
            return
        if chosen is delete_action:
            self.detach()
            return

        field_name = action_to_field.get(chosen)
        if field_name is not None:
            setattr(self.state, field_name, not getattr(self.state, field_name))
            self._on_state_changed()

    def _on_state_changed(self):
        """Repaint after a toggle change and, if a source is already
        designated, immediately re-resolve and re-arm live sync."""
        self.update()
        # Re-arm live sync immediately if a source is already designated -
        # matches the "re-armed whenever the toggle state ... changes" rule.
        if self.last_source_node is not None:
            plan = self._current_plan()
            self.live_sync.rearm(self.last_source_node.source_path if plan.watch else None)

    # -- flow triggering -----------------------------------------------------

    def trigger_flow(self, source_node) -> flow.FlowPlan:
        """Attempt a flow with `source_node` designated as the signal source.

        Always re-resolves and re-arms `live_sync` afterward, per the
        `rearm()`-after-every-`trigger_flow()` call pattern - even if the
        flow itself fails (e.g. `assert_not_original()` refusing a write, or
        a read/write I/O error), so a bad attempt doesn't leave live sync in
        a stale state. Failures are reported via `on_error` rather than
        raising into the caller (a Qt context-menu handler, or a debounced
        QFileSystemWatcher callback - both silently swallow/print uncaught
        exceptions by default).
        """
        self.last_source_node = source_node
        source_endpoint = self._endpoint_of(source_node)
        plan = flow.resolve(self.state, source_endpoint)

        nodes = {Endpoint.A: self.node_a, Endpoint.B: self.node_b}
        if plan.legs:
            try:
                flow.execute(plan, nodes)
            except Exception as exc:  # noqa: BLE001 - surface to the status bar, don't crash the event loop
                self._report_error(f"Flow failed: {exc}")

        self.live_sync.rearm(source_node.source_path if plan.watch else None)
        self.update()
        return plan

    def notify_content_edit(self, node) -> None:
        """Real-time-mirroring hook: called on every keystroke from an
        edited node's `_on_widget_text_changed()`.

        Requires an already-designated source (`last_source_node` is not
        None) - real-time mirroring only activates for a wire that already
        has a designated source (Run has been used at least once on that
        wire already); it does not infer direction from whichever node is
        edited first. No-ops otherwise.

        Only fires when the wire's currently-resolved plan is continuous
        (`watch=True`, i.e. the `live-wire`/`live-mirror-wire` shapes) - Off/
        dead wires are correctly excluded since Off always wins per
        `flow.resolve()`, and plain pulse/one-shot (non-watch) shapes are
        meant to fire only on an explicit discrete Run trigger, not on every
        keystroke.

        Propagates only along leg(s) that *originate* from the edited node
        (`leg[0] == endpoint`) - critically NOT the whole plan, so editing
        one side of a Bi wire doesn't also spuriously re-fire the untouched
        side back onto itself.

        Reuses `flow.execute()` completely unchanged.
        """
        if self.last_source_node is None:
            return
        plan = self._current_plan()
        if not plan.watch:
            return
        try:
            endpoint = self._endpoint_of(node)
        except ValueError:
            return
        matching_legs = tuple(leg for leg in plan.legs if leg[0] == endpoint)
        if not matching_legs:
            return
        live_plan = flow.FlowPlan(legs=matching_legs, cadence=plan.cadence, watch=plan.watch, shape=plan.shape)
        nodes = {Endpoint.A: self.node_a, Endpoint.B: self.node_b}
        try:
            flow.execute(live_plan, nodes)
        except Exception as exc:  # noqa: BLE001 - surface to the status bar, don't crash the event loop
            self._report_error(f"Live mirror failed: {exc}")

    def _report_error(self, message: str) -> None:
        """Surface `message` via `on_error` if the owning window set one.

        Falls back to stderr so a failure is never silently swallowed even
        when this wire isn't wired up to a status bar (e.g. constructed
        directly under test, outside of app_canvas.py).
        """
        if self.on_error is not None:
            self.on_error(message)
        else:
            print(f"[wires] {message}", file=sys.stderr)

    def _on_watch_fire(self):
        """Called by LiveSync after its debounce window elapses following a
        detected change to the watched (designated-source) file."""
        if self.last_source_node is not None:
            self.trigger_flow(self.last_source_node)

    # -- lifecycle -----------------------------------------------------------

    def detach(self):
        """Disarm live sync, unregister from both nodes, and remove from scene."""
        self.live_sync.disarm()
        for node in (self.node_a, self.node_b):
            if node is not None and self in node.wires:
                node.wires.remove(self)
        scene = self.scene()
        if scene is not None:
            scene.removeItem(self)
