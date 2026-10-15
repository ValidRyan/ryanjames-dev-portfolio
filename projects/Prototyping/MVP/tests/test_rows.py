"""Widget-level tests for rows.py's RowStack - open-upward row-stack.

Uses the shared session `qapp` fixture from conftest.py (RowStack is a real
QWidget subclass and needs a live QApplication to construct). Deliberately a
cheap smoke-test level of coverage per the spec - not an exhaustive probe of
Qt's own rendering internals.
"""

import sys
from pathlib import Path

_MVP_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_MVP_ROOT / "Folder Replication"))

from PySide6.QtWidgets import QPushButton

import rows
import store_model
from rows import RowStack

SAMPLE_TXT = _MVP_ROOT / "sample" / "sample_input.txt"


def _row_widget(row_stack, index):
    return row_stack._layout.itemAt(index).widget()


def _buttons_in_row(row_stack, index):
    return _row_widget(row_stack, index).findChildren(QPushButton)


def _button_with_text(row_stack, index, text):
    for button in _buttons_in_row(row_stack, index):
        if button.text() == text:
            return button
    raise AssertionError(f"No button labeled {text!r} in row {index}")


def _build_tree():
    """root
       |- FolderA
       |    |- sample_input.txt
       |    |- SubFolder (empty)
       |- FolderB (empty)
    """
    root = store_model.new_root()
    folder_a = store_model.add_folder(root, "FolderA")
    folder_b = store_model.add_folder(root, "FolderB")
    file_a1 = store_model.add_file(folder_a, str(SAMPLE_TXT))
    subfolder = store_model.add_folder(folder_a, "SubFolder")
    return root, folder_a, folder_b, file_a1, subfolder


class _FakeDeleteMenu(rows.QMenu):
    """Stand-in QMenu subclass that always "chooses" Delete without a real
    event loop - see test_operators.py's own note on why a QMenu subclass
    (rather than monkeypatching QMenu.exec directly) is what reliably
    intercepts exec() in this PySide6 build."""

    def exec(self, pos):
        for action in self.actions():
            if action.text() == "Delete":
                return action
        return None


class _FakeContextMenuEvent:
    def globalPos(self):
        return None


def test_only_root_row_shows_initially(qapp):
    root, *_ = _build_tree()
    row_stack = RowStack(root)
    assert row_stack._layout.count() == 1


def test_opening_a_folder_inserts_a_row_above_it(qapp):
    root, folder_a, folder_b, file_a1, subfolder = _build_tree()
    row_stack = RowStack(root)

    root_row_index = row_stack._layout.count() - 1
    _button_with_text(row_stack, root_row_index, "FolderA/").click()

    assert row_stack._open_path == [folder_a]
    assert row_stack._layout.count() == 2
    # The new row sits ABOVE the root row, i.e. at index 0.
    top_row_labels = {button.text() for button in _buttons_in_row(row_stack, 0)}
    assert top_row_labels == {"sample_input.txt", "SubFolder/"}


def test_clicking_the_same_folder_again_collapses_it(qapp):
    root, folder_a, folder_b, file_a1, subfolder = _build_tree()
    row_stack = RowStack(root)

    root_row_index = row_stack._layout.count() - 1
    _button_with_text(row_stack, root_row_index, "FolderA/").click()
    assert row_stack._layout.count() == 2

    new_root_row_index = row_stack._layout.count() - 1
    _button_with_text(row_stack, new_root_row_index, "FolderA/").click()

    assert row_stack._open_path == []
    assert row_stack._layout.count() == 1


def test_opening_a_different_folder_on_the_same_row_replaces_the_open_one(qapp):
    root, folder_a, folder_b, file_a1, subfolder = _build_tree()
    row_stack = RowStack(root)

    root_row_index = row_stack._layout.count() - 1
    _button_with_text(row_stack, root_row_index, "FolderA/").click()
    assert row_stack._open_path == [folder_a]

    new_root_row_index = row_stack._layout.count() - 1
    _button_with_text(row_stack, new_root_row_index, "FolderB/").click()

    assert row_stack._open_path == [folder_b]
    assert row_stack._layout.count() == 2  # FolderB has no children, but its (empty) row still shows


def test_opening_a_different_folder_collapses_deeper_stacked_rows(qapp):
    root, folder_a, folder_b, file_a1, subfolder = _build_tree()
    row_stack = RowStack(root)

    root_row_index = row_stack._layout.count() - 1
    _button_with_text(row_stack, root_row_index, "FolderA/").click()
    row1_index = row_stack._layout.count() - 1 - 1  # the row rendering FolderA's children
    _button_with_text(row_stack, row1_index, "SubFolder/").click()
    assert row_stack._open_path == [folder_a, subfolder]
    assert row_stack._layout.count() == 3

    # Now open a different folder at the ROOT row (depth 0) - should collapse
    # everything stacked above it (FolderA's and SubFolder's rows) and open
    # FolderB instead.
    new_root_row_index = row_stack._layout.count() - 1
    _button_with_text(row_stack, new_root_row_index, "FolderB/").click()

    assert row_stack._open_path == [folder_b]
    assert row_stack._layout.count() == 2


def test_highlight_flag_produces_different_folder_styling(qapp):
    root, folder_a, folder_b, file_a1, subfolder = _build_tree()
    neutral_stack = RowStack(root, highlight_folders=False)
    highlighted_stack = RowStack(root, highlight_folders=True)

    root_row_index = neutral_stack._layout.count() - 1
    neutral_button = _button_with_text(neutral_stack, root_row_index, "FolderA/")
    highlighted_button = _button_with_text(highlighted_stack, root_row_index, "FolderA/")

    assert neutral_button.styleSheet() != highlighted_button.styleSheet()


def test_open_folder_changed_signal_emits_deepest_open_folder(qapp):
    root, folder_a, folder_b, file_a1, subfolder = _build_tree()
    row_stack = RowStack(root)
    received = []
    row_stack.open_folder_changed.connect(lambda folder: received.append(folder))

    root_row_index = row_stack._layout.count() - 1
    _button_with_text(row_stack, root_row_index, "FolderA/").click()

    assert received == [folder_a]
    assert row_stack.current_open_folder() is folder_a


def test_current_open_folder_is_none_when_nothing_open(qapp):
    root, *_ = _build_tree()
    row_stack = RowStack(root)
    assert row_stack.current_open_folder() is None


def test_delete_via_context_menu_removes_item_and_rebuilds(qapp, monkeypatch):
    root, folder_a, folder_b, file_a1, subfolder = _build_tree()
    row_stack = RowStack(root)
    monkeypatch.setattr(rows, "QMenu", _FakeDeleteMenu)

    root_row_index = row_stack._layout.count() - 1
    button = _button_with_text(row_stack, root_row_index, "FolderB/")
    button.contextMenuEvent(_FakeContextMenuEvent())

    assert folder_b not in root.children
    assert row_stack._layout.count() == 1


def test_deleting_a_currently_open_folder_cleans_up_open_path(qapp, monkeypatch):
    root, folder_a, folder_b, file_a1, subfolder = _build_tree()
    row_stack = RowStack(root)

    root_row_index = row_stack._layout.count() - 1
    _button_with_text(row_stack, root_row_index, "FolderA/").click()
    assert row_stack._open_path == [folder_a]

    monkeypatch.setattr(rows, "QMenu", _FakeDeleteMenu)
    new_root_row_index = row_stack._layout.count() - 1
    button = _button_with_text(row_stack, new_root_row_index, "FolderA/")
    button.contextMenuEvent(_FakeContextMenuEvent())

    assert row_stack._open_path == []
    assert folder_a not in root.children


def test_enable_delete_false_disables_the_delete_callback(qapp):
    root, folder_a, folder_b, file_a1, subfolder = _build_tree()
    row_stack = RowStack(root, enable_delete=False)

    root_row_index = row_stack._layout.count() - 1
    button = _button_with_text(row_stack, root_row_index, "FolderA/")

    assert button._on_delete is None


def test_set_root_resets_open_path_by_default(qapp):
    root, folder_a, folder_b, file_a1, subfolder = _build_tree()
    row_stack = RowStack(root)

    root_row_index = row_stack._layout.count() - 1
    _button_with_text(row_stack, root_row_index, "FolderA/").click()
    assert row_stack._open_path == [folder_a]

    new_root = store_model.new_root()
    row_stack.set_root(new_root)

    assert row_stack._open_path == []
    assert row_stack._layout.count() == 1
