"""Tests for live_sync.py - headless, real tmp_path files.

Uses QTest.qWait (never time.sleep) to let Qt's event loop actually process
the debounce QTimer and QFileSystemWatcher signals.
"""

import sys
from pathlib import Path

_MVP_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_MVP_ROOT / "Joint Interactor"))

from PySide6.QtTest import QTest

from live_sync import DEBOUNCE_MS, LiveSync

# Generous ceiling for OS-level file-change notification delivery, which can
# lag well past the debounce window itself under a headless/offscreen QPA
# platform - polling in small increments (rather than one fixed qWait) keeps
# these tests fast on the common path without being flaky on a slow one.
_FIRE_TIMEOUT_MS = 5000
_POLL_STEP_MS = 50

# `addPath()`/`rearm()` returns before the native OS-level watch thread has
# necessarily started observing the directory (most noticeable "cold", on the
# very first watch of a test session) - a write immediately following rearm()
# can race ahead of that and be silently missed. A short settle wait closes
# that window; harmless in real usage, where a human editing a file externally
# takes far longer than this to make the next save.
_ARM_SETTLE_MS = 100


def _wait_until_fired(fired, timeout_ms=_FIRE_TIMEOUT_MS):
    """Poll via QTest.qWait until `fired` is non-empty or `timeout_ms` elapses."""
    waited = 0
    while not fired and waited < timeout_ms:
        QTest.qWait(_POLL_STEP_MS)
        waited += _POLL_STEP_MS
    return fired


def test_rearm_arms_watcher_on_given_path(qapp, tmp_path):
    watched = tmp_path / "watched.txt"
    watched.write_text("initial", encoding="utf-8")

    sync = LiveSync()
    sync.rearm(str(watched))

    assert str(watched) in sync._watcher.files()
    assert sync.watched_path == str(watched)
    sync.disarm()


def test_disarm_clears_watcher(qapp, tmp_path):
    watched = tmp_path / "watched.txt"
    watched.write_text("initial", encoding="utf-8")

    sync = LiveSync()
    sync.rearm(str(watched))
    sync.disarm()

    assert sync._watcher.files() == []
    assert sync.watched_path is None


def test_rearm_with_none_disarms(qapp, tmp_path):
    watched = tmp_path / "watched.txt"
    watched.write_text("initial", encoding="utf-8")

    sync = LiveSync()
    sync.rearm(str(watched))
    sync.rearm(None)

    assert sync._watcher.files() == []
    assert sync.watched_path is None


def test_rearm_swaps_to_new_path(qapp, tmp_path):
    first = tmp_path / "first.txt"
    second = tmp_path / "second.txt"
    first.write_text("a", encoding="utf-8")
    second.write_text("b", encoding="utf-8")

    sync = LiveSync()
    sync.rearm(str(first))
    sync.rearm(str(second))

    assert sync.watched_path == str(second)
    assert str(first) not in sync._watcher.files()
    sync.disarm()


def test_file_change_fires_after_debounce_window(qapp, tmp_path):
    watched = tmp_path / "watched.txt"
    watched.write_text("initial", encoding="utf-8")

    fired = []
    sync = LiveSync(on_fire=lambda: fired.append(True))
    sync.rearm(str(watched))
    QTest.qWait(_ARM_SETTLE_MS)

    watched.write_text("changed", encoding="utf-8")

    _wait_until_fired(fired)

    assert fired == [True]
    sync.disarm()


def test_rapid_changes_are_debounced_into_a_single_fire(qapp, tmp_path):
    watched = tmp_path / "watched.txt"
    watched.write_text("initial", encoding="utf-8")

    fired = []
    sync = LiveSync(on_fire=lambda: fired.append(True))
    sync.rearm(str(watched))
    QTest.qWait(_ARM_SETTLE_MS)

    watched.write_text("change 1", encoding="utf-8")
    QTest.qWait(50)
    watched.write_text("change 2", encoding="utf-8")
    QTest.qWait(50)
    watched.write_text("change 3", encoding="utf-8")

    _wait_until_fired(fired)
    QTest.qWait(DEBOUNCE_MS + 300)  # confirm no further, uncoalesced fires trail in

    assert fired == [True]
    sync.disarm()


def test_delete_recreate_save_pattern_still_fires(qapp, tmp_path):
    """Windows commonly saves via delete+recreate - verify the watcher keeps
    firing across that pattern instead of silently going dead."""
    watched = tmp_path / "watched.txt"
    watched.write_text("initial", encoding="utf-8")

    fired = []
    sync = LiveSync(on_fire=lambda: fired.append(True))
    sync.rearm(str(watched))
    QTest.qWait(_ARM_SETTLE_MS)

    watched.unlink()
    watched.write_text("recreated", encoding="utf-8")

    _wait_until_fired(fired)

    assert len(fired) >= 1
    sync.disarm()


def test_disarm_stops_pending_debounce_from_firing(qapp, tmp_path):
    watched = tmp_path / "watched.txt"
    watched.write_text("initial", encoding="utf-8")

    fired = []
    sync = LiveSync(on_fire=lambda: fired.append(True))
    sync.rearm(str(watched))
    QTest.qWait(_ARM_SETTLE_MS)

    watched.write_text("changed", encoding="utf-8")
    QTest.qWait(50)  # inside the debounce window - timer is pending, not fired yet
    sync.disarm()

    QTest.qWait(DEBOUNCE_MS + 300)

    assert fired == []
