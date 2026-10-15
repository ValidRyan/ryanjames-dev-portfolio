"""Regression tests for the Qt lifetime helpers in write_testkit.py (the order-dependent crash).

The full suite once died with "Windows fatal exception: access violation" (exit
139) at `QTest.qWait(30)` right after `edit.show()` in `test_write_overlay.py`,
but only when Folder Replication's `test_rows.py` had run first. Bisect: exactly
`test_rows.py::test_set_root_resets_open_path_by_default` followed by ANY Qt
event spin (no Write code needed). Mechanism, reproduced here with no Store code:

- a top-level widget whose children are wired with `lambda: self...` is kept
  alive only by those closures (a cycle through C++ that `gc` cannot see);
- it rebuilds its rows with `deleteLater()`, and its FINAL rows hold no closure;
- the next event spin runs the queued delete: the old row's buttons die, their
  closures drop the last reference to the owner's wrapper, and PySide deletes
  the owner from inside the destructor of its own child.

`_SelfHoldingStack` below has exactly that shape. The first test asserts the
precondition (so it can never pass vacuously), then calls
`write_testkit.settle_leftover_qt()`; without the reference pin inside that
helper this very line kills the pytest process with the same access violation,
deterministically (checked by neutering the helper). The other tests cover the
deterministic-disposal half of the helpers. Source stays ASCII.
"""

import gc
import weakref

import pytest
import shiboken6
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QHBoxLayout, QPushButton, QTextEdit, QVBoxLayout, QWidget

import write_testkit
from write_testkit import write_qt  # noqa: F401  (fixture: see write_testkit's Qt lifetime rules)

pytestmark = pytest.mark.usefixtures("write_qt")


class _SelfHoldingStack(QWidget):
    """The shape of Folder Replication's `rows.RowStack`: rebuilt rows, `deleteLater`, closure over `self`."""

    def __init__(self):
        super().__init__()
        self._layout = QVBoxLayout(self)
        self.rebuild(buttons=True)

    def rebuild(self, buttons):
        while self._layout.count():
            widget = self._layout.takeAt(0).widget()
            if widget is not None:
                widget.deleteLater()  # queued, not run: nothing spins the loop here
        row = QWidget()
        row_layout = QHBoxLayout(row)
        if buttons:
            button = QPushButton("x")
            button.clicked.connect(lambda checked=False: self.rebuild(buttons=False))  # closes over self: the leak
            row_layout.addWidget(button)
        self._layout.addWidget(row)


def _leak_a_self_holding_stack(events):
    """Build the hazard and drop every local reference to it; return only a weakref."""
    stack = _SelfHoldingStack()
    old_row = stack._layout.itemAt(0).widget()
    old_row.destroyed.connect(lambda *_: events.append("old row destroyed"))
    stack.rebuild(buttons=False)  # old row: deleteLater() queued; the final row has no closure of its own
    return weakref.ref(stack)


def test_a_leaked_self_holding_owner_with_a_queued_delete_cannot_crash_the_next_event_spin(qapp):
    events = []
    stack_ref = _leak_a_self_holding_stack(events)

    # preconditions: the exact hazard shape (a cycle gc cannot break, a delete still queued)
    gc.collect()
    assert stack_ref() is not None, "only the old row's button closure keeps the owner alive"
    assert events == [], "the old row's deleteLater() has not run yet"

    write_testkit.settle_leftover_qt()  # without the wrapper pin this line is an access violation

    assert events == ["old row destroyed"], "the queued delete really ran"
    assert stack_ref() is None, "the leaked owner was then released cleanly, after (not inside) the flush"
    QTest.qWait(30)  # a later spin has nothing foreign left to execute
    QTest.qWait(30)


def test_settle_is_idempotent_and_leaves_live_widgets_alone(qapp):
    keeper = write_testkit.track_qt(QTextEdit())
    keeper.resize(120, 80)
    keeper.show()
    write_testkit.settle_leftover_qt()
    write_testkit.settle_leftover_qt()
    assert shiboken6.isValid(keeper) and keeper.isVisible()  # a live, referenced widget is not touched


def test_dispose_tracked_deletes_at_once_even_when_closures_keep_the_wrapper_alive(qapp):
    stack = write_testkit.track_qt(_SelfHoldingStack())
    shown = write_testkit.track_qt(QTextEdit())
    shown.resize(120, 80)
    shown.show()
    assert shiboken6.isValid(stack) and shiboken6.isValid(shown)

    write_testkit.dispose_tracked()

    assert not shiboken6.isValid(stack) and not shiboken6.isValid(shown)
    write_testkit.dispose_tracked()  # nothing left to do, and no error
    QTest.qWait(10)  # nothing of theirs is left in the event queue


def test_track_qt_refuses_to_run_without_the_fixture(monkeypatch):
    monkeypatch.setattr(write_testkit, "_SCOPE_OPEN", False)
    with pytest.raises(RuntimeError, match="write_qt"):
        write_testkit.track_qt(object())
    assert write_testkit._TRACKED == []
