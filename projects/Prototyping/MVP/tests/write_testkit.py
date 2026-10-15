"""Shared helpers for the SAP Modifier tests (no test functions here).

Imported by `test_write_*.py` as `import write_testkit` (pytest puts this
folder on `sys.path`). Everything writes only under a path the CALLER passes
(always a `tmp_path`); nothing here touches `Prototyping/MVP/sample/`. PySide6 is
imported lazily INSIDE the Qt lifetime helpers, so importing this module never
pulls Qt in and the Qt-free test files stay Qt-free.

- `make_write(path, lines)` - write a minimal valid `.mwrite` file straight as
  JSON (deliberately NOT through `write_format.save_write`, so loader tests do
  not depend on the saver).
- `fake_png_header(width, height)` - a signature + IHDR-only byte string: enough
  for the Qt-free header checks (`png_dimensions`), not a decodable image.
- `make_dir_link(link, target)` - a directory junction (`_winapi.CreateJunction`,
  no admin rights) or, off Windows, a symlink; returns False when unsupported so
  the caller can `pytest.skip`. `remove_dir_link(link)` deletes one safely.
- `tree_fixture(root)` - a small on-disk tree covering every file kind.
- Qt lifetime helpers (below): the `write_qt` fixture, `track_qt`, `dispose_qt`,
  `settle_leftover_qt`.

Qt lifetime rules for every Write test that builds widgets
-----------------------------------------------------------
Every MVP's tests run in ONE process against ONE session `QApplication`, so
objects an earlier test file leaked are still alive, with their `deleteLater()`
events still queued, while a Write test runs. The Write suite once died with an
access violation (exit 139) at `QTest.qWait(30)` right after `edit.show()` in
`test_write_overlay.py` - a plain `QTextEdit`, no Write code involved. Root
cause (bisected to `test_rows.py::test_set_root_resets_open_path_by_default`,
reproduced with no Write code and then with no Store code at all):

1. `QTest.qWait`, `processEvents` and `sendPostedEvents(None, DeferredDelete)`
   execute EVERY queued `deleteLater()` in the process, not just yours.
2. A parentless (top-level) widget whose children are wired with
   `clicked.connect(lambda ...: self...)` is kept alive by those closures alone:
   the reference cycle runs through C++, so `gc.collect()` cannot free it.
3. Such a widget rebuilds its rows with `deleteLater()`. When the queued delete
   runs, the old row's buttons die, their closures drop the LAST reference to
   the owner's Python wrapper, and PySide deletes the owner's C++ object from
   inside the destructor of its own child: access violation.
4. It only bites when the widget's final rows hold no closure of their own (an
   empty row), which is why 11 of the 12 `test_rows.py` tests never triggered it.
   The pre-existing 202 tests pass only by file order: `test_live_sync.py`, the
   one original file that spins the loop, sorts BEFORE `test_rows.py`. Run
   `test_rows.py` first and it crashed on its own (no Write file involved). The
   cure was applied upstream (Folder Replication's `rows.py` now closes over a weakref, not
   over `self`); `settle_leftover_qt()` stays as defence in depth so the Write
   tests never depend on another MVP's widgets being well behaved.

Rules (all enforced by the `write_qt` fixture where a fixture can enforce them):

- Request `write_qt` for every Write test that touches widgets or spins the
  event loop: `pytestmark = pytest.mark.usefixtures("write_qt")` after
  `from write_testkit import write_qt  # noqa: F401`. Its setup runs
  `settle_leftover_qt()` BEFORE the test can flush anything.
- Register every top-level widget you create with `track_qt(widget)` (it returns
  the widget); the fixture deletes it deterministically at teardown, so nothing
  of yours is ever left for a later file to trip over. Never rely on refcounts,
  `gc.collect()` or `deleteLater()` to dispose a test widget.
- In production widgets, connect BOUND METHODS of QObject subclasses
  (`button.clicked.connect(self._on_clicked)`), not lambdas that close over
  `self`: a bound-method connection does not keep the wrapper alive (verified),
  so the cycle in (2) never forms. Parent child widgets to their owner.
- Never create QObject-derived or paint-device objects (widgets, documents,
  highlighters, painters, pixmaps, timers) at module level: they outlive every
  test and are built before the QApplication exists. Build them inside the test
  (or a fixture that depends on `qapp`). Plain geometry/colour value types
  (`QColor`, `QRectF`, `QPointF`) are fine as module constants; never a `QFont`.
- Keep a `QPainter` short: `painter.end()` in a `finally`, before its
  `QImage`/widget can die. Do not use a `QTextDocument`, layout, block or
  fragment wrapper after the widget that owns it was disposed: those wrappers
  do not own the C++ object.
- A `QSyntaxHighlighter` is a child of its document: it dies with it. Never touch
  its wrapper after the document or editor is gone.
- The offscreen platform here has NO fonts (Qt prints "Cannot find font
  directory"): every glyph, spaces included, is a solid-looking box. Pixel
  tests must measure inside the run under test and never assume whitespace is
  ink-free.
"""

import json
import os
import struct
import zlib
from pathlib import Path

import pytest

# Widgets registered by `track_qt` during the running test, and whether the
# `write_qt` fixture is active (track_qt refuses to run without it, so a test
# cannot register a widget that would never be disposed).
_TRACKED: list = []
_SCOPE_OPEN = False


def make_write(path, lines=()) -> Path:
    """Write a valid `.mwrite` file at `path` (parents created) and return the Path.

    `lines` items are either a text string (indent 0) or an `(indent, text)`
    pair; each becomes one paragraph with a single untagged run. An empty
    `lines` still yields one empty paragraph, like the real saver.
    """
    paragraphs = []
    for item in lines:
        indent, text = (0, item) if isinstance(item, str) else item
        paragraphs.append({"indent": indent, "runs": [{"text": text}] if text else []})
    if not paragraphs:
        paragraphs.append({"indent": 0, "runs": []})
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps({"version": 1, "paragraphs": paragraphs}), encoding="utf-8")
    return target


def fake_png_header(width: int, height: int) -> bytes:
    """PNG signature + a valid-CRC IHDR chunk declaring `width` x `height` (RGBA, 8-bit).

    No IDAT/IEND: it can never decode, which is the point for the
    dimension-bomb tests (the header alone must be enough to refuse it).
    """
    ihdr = struct.pack(">II5B", width, height, 8, 6, 0, 0, 0)
    crc = zlib.crc32(b"IHDR" + ihdr) & 0xFFFFFFFF
    return b"\x89PNG\r\n\x1a\n" + struct.pack(">I", len(ihdr)) + b"IHDR" + ihdr + struct.pack(">I", crc)


def make_dir_link(link, target) -> bool:
    """Create a directory link at `link` pointing at `target`; return False if unsupported.

    On Windows this is an NTFS junction via `_winapi.CreateJunction` (works
    without elevation, unlike `os.symlink`); elsewhere a directory symlink.
    Any failure (missing API, no privilege, unsupported filesystem) -> False.
    """
    link, target = str(link), str(target)
    if os.name == "nt":
        try:
            import _winapi

            _winapi.CreateJunction(target, link)
            return True
        except (OSError, AttributeError, ImportError):
            pass  # fall through to a symlink attempt (needs privilege / developer mode)
    try:
        os.symlink(target, link, target_is_directory=True)
        return True
    except (OSError, NotImplementedError):
        return False


def remove_dir_link(link) -> None:
    """Best-effort removal of a link made by `make_dir_link` WITHOUT touching its target.

    `os.rmdir` removes a junction (or directory symlink) itself; a recursive
    delete of a temp tree that still holds a junction back into it could
    otherwise walk in circles. Never raises.
    """
    try:
        os.rmdir(str(link))
    except OSError:
        try:
            os.unlink(str(link))
        except OSError:
            pass


def tree_fixture(root) -> dict:
    """Build a small tree under `root` and return a dict of the interesting paths.

    Layout: `a.txt`, `sub/b.txt`, `sub/c.mwrite`, `sub dir/it's.txt`,
    `notes.mstore/{one.txt,two.mwrite,sub/deep.txt}`, `stub.mdoc`, `stub.mprog`
    and `fake.mstore` (a regular FILE with a store extension). Keys of the
    returned dict: root, a, sub, sub_b, sub_c, spaced, store, store_one,
    store_two, store_sub, store_deep, doc, prog, fake_store.
    """
    root = Path(root)
    (root / "sub").mkdir(parents=True, exist_ok=True)
    (root / "sub dir").mkdir(exist_ok=True)
    (root / "notes.mstore" / "sub").mkdir(parents=True, exist_ok=True)
    (root / "a.txt").write_text("alpha", encoding="utf-8")
    (root / "sub" / "b.txt").write_text("bravo", encoding="utf-8")
    make_write(root / "sub" / "c.mwrite", ["Info: in c"])
    (root / "sub dir" / "it's.txt").write_text("quoted", encoding="utf-8")
    (root / "notes.mstore" / "one.txt").write_text("one", encoding="utf-8")
    make_write(root / "notes.mstore" / "two.mwrite", ["Info: in two"])
    (root / "notes.mstore" / "sub" / "deep.txt").write_text("deep", encoding="utf-8")
    (root / "stub.mdoc").write_text("doc line one\ndoc line two\n", encoding="utf-8")
    (root / "stub.mprog").write_text("prog line\n", encoding="utf-8")
    (root / "fake.mstore").write_text("not a directory", encoding="utf-8")
    return {
        "root": root,
        "a": root / "a.txt",
        "sub": root / "sub",
        "sub_b": root / "sub" / "b.txt",
        "sub_c": root / "sub" / "c.mwrite",
        "spaced": root / "sub dir" / "it's.txt",
        "store": root / "notes.mstore",
        "store_one": root / "notes.mstore" / "one.txt",
        "store_two": root / "notes.mstore" / "two.mwrite",
        "store_sub": root / "notes.mstore" / "sub",
        "store_deep": root / "notes.mstore" / "sub" / "deep.txt",
        "doc": root / "stub.mdoc",
        "prog": root / "stub.mprog",
        "fake_store": root / "fake.mstore",
    }


# -- Qt lifetime helpers (see the module docstring for the rules) -------------------


def settle_leftover_qt() -> None:
    """Run every `deleteLater()` other test files left queued, without crashing on them.

    Pins a strong reference to every top-level widget wrapper first
    (`QApplication.topLevelWidgets()` returns the SAME wrapper objects), then
    executes the queued deferred deletes, then releases the pins. While pinned no
    leaked owner can be finalised from inside the destructor of one of its own
    children (the crash described in the module docstring); afterwards nothing is
    left queued, so a later `QTest.qWait` in a test has nothing foreign to run.
    Idempotent and cheap; the `write_qt` fixture calls it before each test. A
    leaked widget is deleted only when its wrapper is not otherwise referenced -
    exactly what it would have got had the loop run normally.
    """
    from PySide6.QtCore import QEvent
    from PySide6.QtWidgets import QApplication

    pinned = QApplication.topLevelWidgets()
    QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    del pinned  # released only AFTER the flush


def dispose_qt(*widgets) -> None:
    """Delete the C++ objects behind `widgets` right now, skipping any already gone.

    Uses `shiboken6.delete` (an immediate C++ delete, independent of Python
    refcounts, closure cycles and the event loop; the object's own queued events
    go with it). The wrappers stay valid Python objects but become invalid Qt
    objects, so do not touch them afterwards.
    """
    import shiboken6

    for widget in widgets:
        if widget is not None and shiboken6.isValid(widget):
            shiboken6.delete(widget)


def track_qt(widget):
    """Register a top-level `widget` for deterministic deletion at the end of the test; return it.

    Raises RuntimeError outside the `write_qt` fixture, so a forgotten fixture
    fails loudly instead of leaking the widget into the next test file.
    """
    if not _SCOPE_OPEN:
        raise RuntimeError('track_qt() needs the write_qt fixture: pytestmark = pytest.mark.usefixtures("write_qt")')
    _TRACKED.append(widget)
    return widget


def dispose_tracked() -> None:
    """Delete every widget registered with `track_qt` (last registered first) and forget them.

    The `write_qt` fixture calls this at teardown; a test may call it earlier to
    check that its widgets are really gone. Safe to call twice.
    """
    tracked = list(reversed(_TRACKED))
    _TRACKED.clear()
    dispose_qt(*tracked)


@pytest.fixture
def write_qt(qapp):
    """Per-test Qt hygiene for the SAP Modifier: settle leftovers on the way in, dispose yours on the way out.

    Import it into a test module (`from write_testkit import write_qt  # noqa:
    F401`) and apply it to every test with `pytestmark = pytest.mark.usefixtures(
    "write_qt")`. Setup: `settle_leftover_qt()`. Teardown: `dispose_tracked()`, so
    every widget passed to `track_qt` is deleted even when the test failed.
    """
    global _SCOPE_OPEN
    settle_leftover_qt()
    _SCOPE_OPEN = True
    try:
        yield
    finally:
        _SCOPE_OPEN = False
        dispose_tracked()
