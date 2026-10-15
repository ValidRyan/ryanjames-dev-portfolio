"""Tests for app_write.py - the SAP Modifier window (a thin shell over the finished pieces).

Six tests, one per thing the window itself owns: its construction, the Save /
Open round trip, a refused Open, the unsaved-changes prompt, Run, and the
debounced refresh. The pieces underneath have their own test files, so nothing
here re-tests them.

Every test runs under the `write_qt` fixture and registers the window it builds
with `write_testkit.track_qt` (see write_testkit's Qt lifetime rules). File
dialogs and the message box are stubbed by the `dialogs` fixture, which patches
the static methods on the classes the window calls (the module-level
`QFileDialog` / `QMessageBox` names in `app_write`); a dialog a test did not
answer FAILS instead of blocking the run. Files only ever go under `tmp_path`,
never into `sample/`. Source stays ASCII.
"""

import sys
from pathlib import Path
from types import SimpleNamespace

_MVP_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_MVP_ROOT / "SAP Modifier"))

import pytest
from PySide6.QtGui import QKeySequence, QTextCursor
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QMessageBox, QToolBar

import app_write
import write_testkit
from write_paths import ext_for
from write_resolve import iter_nodes
from write_testkit import make_write, write_qt  # noqa: F401  (fixture: see write_testkit's Qt lifetime rules)

pytestmark = pytest.mark.usefixtures("write_qt")

TOOLBAR_NAMES = [
    "New", "Open", "Save", "Save As",
    "Bold", "Line", "Dash", "Italics", "Spread", "Narrow", "Embed", "Un-embed", "Indent", "Outdent",
    "Insert reference...", "Insert Store reference...", "Run", "Refresh",
]  # fmt: skip


@pytest.fixture
def dialogs(monkeypatch):
    """Stub the dialogs the window opens; `answers[name]` is what a dialog returns, `calls` what was opened.

    A dialog whose name has no answer raises AssertionError (a real modal dialog would hang the run).
    """
    state = SimpleNamespace(answers={}, calls=[])

    def stub(name):
        def call(*args, **kwargs):
            if name not in state.answers:
                raise AssertionError(f"unexpected dialog: {name}")
            state.calls.append(name)
            return state.answers[name]

        return staticmethod(call)

    monkeypatch.setattr(app_write.QFileDialog, "getOpenFileName", stub("getOpenFileName"))
    monkeypatch.setattr(app_write.QFileDialog, "getSaveFileName", stub("getSaveFileName"))
    monkeypatch.setattr(app_write.QFileDialog, "getExistingDirectory", stub("getExistingDirectory"))
    monkeypatch.setattr(app_write.QMessageBox, "question", stub("question"))
    return state


def make_window():
    return write_testkit.track_qt(app_write.MainWindow())


def test_window_builds_with_title_size_layout_and_toolbar(dialogs):
    window = make_window()

    assert window.windowTitle() == "Go - SAP Modifier: Script Processor"
    assert (window.width(), window.height()) == (1000, 700)
    toolbars = window.findChildren(QToolBar)
    assert len(toolbars) == 1
    assert [a.text() for a in toolbars[0].actions() if not a.isSeparator()] == TOOLBAR_NAMES

    actions = window.actions_by_name
    assert [actions[n].isCheckable() for n in ("Bold", "Line", "Dash", "Italics")] == [True, True, True, False]
    portable = QKeySequence.SequenceFormat.PortableText
    shortcuts = {name: actions[name].shortcut().toString(portable) for name in ("Save", "Save As", "Bold", "Run", "Italic right")}
    assert shortcuts == {"Save": "Ctrl+S", "Save As": "Ctrl+Shift+S", "Bold": "Ctrl+B", "Run": "F5", "Italic right": "Ctrl+I"}

    assert window.run_log.isReadOnly() and window.run_log.maximumBlockCount() == 5000
    assert window.status_label.text() == "Clarity 0 | Noise 0 | Comments 0 | Saved"
    window._report_error("boom")
    assert window.statusBar().currentMessage() == "Error: boom"


def test_save_as_then_edit_then_save_clears_modified_and_open_round_trips(dialogs, tmp_path):
    suffix = ext_for("write")
    window = make_window()
    window.editor.insertPlainText("Phrase: hello")
    assert window.editor.document().isModified() and window.status_label.text().endswith("Modified")

    dialogs.answers["getSaveFileName"] = (str(tmp_path / "script"), "")  # no extension typed: the window adds it
    assert window.save_as() is True
    saved = tmp_path / f"script{suffix}"
    assert saved.is_file() and window._path == str(saved)
    assert not window.editor.document().isModified() and window.status_label.text().endswith("Saved")

    window.editor.selectAll()
    window.editor.toggle_bold()
    window.editor.moveCursor(QTextCursor.MoveOperation.End)
    window.editor.insertPlainText("\nGoals: two")
    window.editor.indent_selection(1)
    assert window.editor.document().isModified()

    dialogs.answers.pop("getSaveFileName")  # a plain Save must not ask for a name again
    assert window.save() is True
    assert not window.editor.document().isModified() and window.status_label.text().endswith("Saved")

    other = make_window()
    dialogs.answers["getOpenFileName"] = (str(saved), "")
    other.open_write()
    expected = window.editor.to_model()
    loaded = other.editor.to_model()
    assert loaded == expected  # text, bold and indent all survived the file
    assert loaded.paragraphs[0].runs[0].tags.get("bold") and loaded.paragraphs[1].indent == 1
    assert other._path == str(saved) and not other.editor.document().isModified()
    assert dialogs.calls == ["getSaveFileName", "getOpenFileName"]


def test_opening_an_invalid_file_leaves_the_window_and_both_files_untouched(dialogs, tmp_path):
    good = make_write(tmp_path / f"good{ext_for('write')}", ["Phrase: keep me"])
    bad = tmp_path / f"bad{ext_for('write')}"
    bad.write_bytes(b"this is { not json")
    good_bytes, bad_bytes = good.read_bytes(), bad.read_bytes()
    window = make_window()
    assert window.load_path(str(good)) is True
    outline = window.editor.outline()
    assert outline == [(0, "Phrase: keep me")]

    dialogs.answers["getOpenFileName"] = (str(bad), "")
    window.open_write()

    assert window.statusBar().currentMessage().startswith("Error:")
    assert window.editor.outline() == outline
    assert window._path == str(good) and not window.editor.document().isModified()
    assert good.read_bytes() == good_bytes and bad.read_bytes() == bad_bytes


def test_closing_a_modified_window_prompts_and_cancel_keeps_it_open(dialogs):
    window = make_window()
    window.show()
    window.editor.insertPlainText("unsaved work")

    dialogs.answers["question"] = QMessageBox.StandardButton.Cancel
    assert window.close() is False
    assert window.isVisible() and window.editor.document().isModified()

    dialogs.answers["question"] = QMessageBox.StandardButton.Discard
    assert window.close() is True
    assert not window.isVisible()
    assert dialogs.calls == ["question", "question"]


def test_run_logs_one_not_implemented_line_per_instruction_node(dialogs):
    window = make_window()
    window.editor.insertPlainText("Phrase: one\nGoals: two")

    window.actions_by_name["Run"].trigger()

    lines = window.run_log.toPlainText().splitlines()
    nodes = [node for node in iter_nodes(window.last_result) if node.keyword]
    assert len(nodes) == 2 and len(lines) == 2
    assert all(line.endswith("not implemented yet") for line in lines)
    assert lines[0].startswith("Phrase (line 1") and lines[1].startswith("Goals (line 2")


def test_typing_refreshes_the_tree_and_counts_once_the_debounce_has_passed(dialogs):
    window = make_window()
    before = window.last_result

    window.editor.insertPlainText("Phrase: one\nGoals: two\nprose is noise\n# a comment")
    assert window.last_result is before  # debounced: nothing has run yet
    QTest.qWait(500)  # past the 250 ms debounce

    assert window.last_result is not before
    counts = window.last_result.counts
    assert (counts.clarity, counts.noise, counts.comment) == (2, 1, 1)
    assert window.status_label.text() == "Clarity 2 | Noise 1 | Comments 1 | Modified"
    assert window.tree.topLevelItem(0).childCount() == 2
