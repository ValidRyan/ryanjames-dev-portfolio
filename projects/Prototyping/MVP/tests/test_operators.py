"""Tests for operators.py - headless, against real sample/ fixtures.

Uses the shared session `qapp` fixture from conftest.py (QT_QPA_PLATFORM
already set to offscreen there) since OperatorNode is a real QGraphicsObject
subclass and needs a live QApplication to construct.
"""

import shutil
import sys
from pathlib import Path
from unittest.mock import Mock

_MVP_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_MVP_ROOT / "File Recompiler"))
sys.path.insert(0, str(_MVP_ROOT / "Joint Interactor"))
sys.path.insert(0, str(_MVP_ROOT))

import docx_extract
import operators
from operators import DocxOperatorNode, TxtOperatorNode
from sample.generate_sample import PARAGRAPHS as SAMPLE_PARAGRAPHS

SAMPLE_DIR = _MVP_ROOT / "sample"
SAMPLE_DOCX = SAMPLE_DIR / "sample_input.docx"
SAMPLE_TXT = SAMPLE_DIR / "sample_input.txt"


def test_docx_operator_node_loads_content_via_docx_extract(qapp):
    node = DocxOperatorNode(str(SAMPLE_DOCX))
    assert node.content == "\n".join(SAMPLE_PARAGRAPHS)
    assert node.kind == "docx"
    assert node.source_path == str(SAMPLE_DOCX)


def test_txt_operator_node_loads_content_directly(qapp):
    node = TxtOperatorNode(str(SAMPLE_TXT))
    assert node.content == SAMPLE_TXT.read_text(encoding="utf-8")
    assert node.kind == "txt"


def test_docx_operator_node_export_path_defaults_to_same_type_export(qapp):
    node = DocxOperatorNode(str(SAMPLE_DOCX))
    assert node.export_path == str(SAMPLE_DOCX.with_name("sample_input.export.docx"))


def test_txt_operator_node_export_path_defaults_to_same_type_export(qapp):
    node = TxtOperatorNode(str(SAMPLE_TXT))
    assert node.export_path == str(SAMPLE_TXT.with_name("sample_input.export.txt"))


def test_docx_operator_node_write_export_does_not_touch_original(qapp, tmp_path):
    # Copy the sample docx into tmp_path so any write happens outside sample/.
    tmp_docx = tmp_path / "sample_input.docx"
    shutil.copy(SAMPLE_DOCX, tmp_docx)
    original_bytes = tmp_docx.read_bytes()

    node = DocxOperatorNode(str(tmp_docx))
    written_path = node.write_export("new content")

    assert Path(written_path) == tmp_docx.with_name("sample_input.export.docx")
    assert tmp_docx.read_bytes() == original_bytes  # original untouched
    assert docx_extract.extract_text(written_path) == "new content"


def test_txt_operator_node_write_export_does_not_touch_original(qapp, tmp_path):
    tmp_txt = tmp_path / "sample_input.txt"
    shutil.copy(SAMPLE_TXT, tmp_txt)
    original_text = tmp_txt.read_text(encoding="utf-8")

    node = TxtOperatorNode(str(tmp_txt))
    written_path = node.write_export("brand new text")

    assert Path(written_path) == tmp_txt.with_name("sample_input.export.txt")
    assert Path(written_path).read_text(encoding="utf-8") == "brand new text"
    assert tmp_txt.read_text(encoding="utf-8") == original_text  # original untouched


def test_write_export_dispatch_is_by_export_path_extension_not_node_kind(qapp, tmp_path):
    node = TxtOperatorNode(str(SAMPLE_TXT))
    node.export_path = str(tmp_path / "redirected.docx")

    written_path = node.write_export("txt node writing docx content")

    assert docx_extract.extract_text(written_path) == "txt node writing docx content"


def test_node_wires_list_starts_empty(qapp):
    node = DocxOperatorNode(str(SAMPLE_DOCX))
    assert node.wires == []


def test_run_reloads_content_and_triggers_attached_wires(qapp):
    node = DocxOperatorNode(str(SAMPLE_DOCX))

    calls = []

    class _FakeWire:
        def trigger_flow(self, source):
            calls.append(source)

    fake_wire = _FakeWire()
    node.wires.append(fake_wire)

    node.run()

    assert calls == [node]


def test_delete_detaches_every_attached_wire(qapp):
    node = DocxOperatorNode(str(SAMPLE_DOCX))

    detached = []

    class _FakeWire:
        def detach(self):
            detached.append(True)

    node.wires.append(_FakeWire())
    node.wires.append(_FakeWire())

    node.delete()

    assert detached == [True, True]


def test_base_operator_node_read_source_not_implemented(qapp):
    import pytest

    from operators import OperatorNode

    # OperatorNode itself has no read_source() implementation - constructing
    # one directly should surface that at load time.
    with pytest.raises(NotImplementedError):
        OperatorNode(str(SAMPLE_DOCX))


def test_export_as_valid_choice_updates_export_path(qapp, monkeypatch, tmp_path):
    node = DocxOperatorNode(str(SAMPLE_DOCX))
    chosen_path = str(tmp_path / "chosen.docx")

    monkeypatch.setattr(
        operators.QFileDialog, "getSaveFileName", staticmethod(lambda *a, **k: (chosen_path, ""))
    )

    result = node.export_as()

    assert result == chosen_path
    assert node.export_path == chosen_path


def test_export_as_cancelled_returns_none_and_leaves_export_path_unchanged(qapp, monkeypatch):
    node = DocxOperatorNode(str(SAMPLE_DOCX))
    default_export_path = node.export_path

    monkeypatch.setattr(
        operators.QFileDialog, "getSaveFileName", staticmethod(lambda *a, **k: ("", ""))
    )

    result = node.export_as()

    assert result is None
    assert node.export_path == default_export_path


def test_export_as_collision_warns_and_reprompts(qapp, monkeypatch, tmp_path):
    # Uses a tmp_path copy (not SAMPLE_DOCX directly) - export_as() now
    # writes immediately on a valid choice, and the real sample/ fixtures
    # must never be touched by a test run.
    tmp_docx = tmp_path / "sample_input.docx"
    shutil.copy(SAMPLE_DOCX, tmp_docx)
    node = DocxOperatorNode(str(tmp_docx))
    valid_path = str(tmp_path / "valid_choice.docx")
    call_count = [0]

    def fake_get_save_file_name(*args, **kwargs):
        call_count[0] += 1
        if call_count[0] == 1:
            return (node.source_path, "")  # collides with its own known original
        return (valid_path, "")

    warnings = []
    monkeypatch.setattr(
        operators.QFileDialog, "getSaveFileName", staticmethod(fake_get_save_file_name)
    )
    monkeypatch.setattr(
        operators.QMessageBox, "warning", staticmethod(lambda *a, **k: warnings.append(True))
    )

    result = node.export_as()

    assert len(warnings) == 1
    assert call_count[0] == 2
    assert result == valid_path
    assert node.export_path == valid_path


def test_context_menu_has_the_full_six_item_list(qapp, monkeypatch):
    # NOTE: PySide6 6.11's QMenu.exec is backed by a metaclass-level
    # exec/exec_ compatibility shim, so plain `monkeypatch.setattr(QMenu,
    # "exec", fake_fn)` on the class itself is silently ignored at call
    # time (verified empirically against this environment's PySide6
    # build - the real C++ exec() still runs and raises on a non-QPoint
    # argument). Subclassing QMenu and overriding exec() there participates
    # in normal Python MRO lookup and reliably intercepts the call, so
    # operators.QMenu is monkeypatched to such a subclass instead, keeping
    # addAction()/actions() fully real.
    node = DocxOperatorNode(str(SAMPLE_DOCX))
    captured = []

    class _FakeMenu(operators.QMenu):
        def exec(self, pos):
            # menu.addSeparator() adds an action whose .text() is "" and
            # .isSeparator() is True - filtered out here so the expected
            # list below doesn't need an empty-string entry.
            captured.extend(action.text() for action in self.actions() if not action.isSeparator())
            return None

    monkeypatch.setattr(operators, "QMenu", _FakeMenu)

    class _FakeEvent:
        def screenPos(self):
            return None

    node.contextMenuEvent(_FakeEvent())

    assert captured == [
        "Run",
        "Export",
        "Export As...",
        "Display Contents",
        "Reload from Source",
        "Delete",
    ]


def test_context_menu_shows_hide_contents_label_when_panel_is_open(qapp, monkeypatch):
    node = DocxOperatorNode(str(SAMPLE_DOCX))
    node.toggle_display_contents()  # opens the panel

    captured = []

    class _FakeMenu(operators.QMenu):
        def exec(self, pos):
            captured.extend(action.text() for action in self.actions() if not action.isSeparator())
            return None

    monkeypatch.setattr(operators, "QMenu", _FakeMenu)

    class _FakeEvent:
        def screenPos(self):
            return None

    node.contextMenuEvent(_FakeEvent())

    assert "Hide Contents" in captured
    assert "Display Contents" not in captured


def test_context_menu_export_as_action_invokes_export_as(qapp, monkeypatch):
    # See NOTE above test_context_menu_has_the_full_six_item_list for why
    # QMenu is replaced with a subclass rather than exec being monkeypatched
    # directly on the class.
    node = DocxOperatorNode(str(SAMPLE_DOCX))

    class _FakeMenu(operators.QMenu):
        def exec(self, pos):
            for action in self.actions():
                if action.text() == "Export As...":
                    return action
            return None

    monkeypatch.setattr(operators, "QMenu", _FakeMenu)

    calls = []
    monkeypatch.setattr(node, "export_as", lambda: calls.append(True))

    class _FakeEvent:
        def screenPos(self):
            return None

    node.contextMenuEvent(_FakeEvent())

    assert calls == [True]


def test_context_menu_export_action_invokes_export(qapp, monkeypatch):
    node = DocxOperatorNode(str(SAMPLE_DOCX))

    class _FakeMenu(operators.QMenu):
        def exec(self, pos):
            for action in self.actions():
                if action.text() == "Export":
                    return action
            return None

    monkeypatch.setattr(operators, "QMenu", _FakeMenu)

    calls = []
    monkeypatch.setattr(node, "export", lambda: calls.append(True))

    class _FakeEvent:
        def screenPos(self):
            return None

    node.contextMenuEvent(_FakeEvent())

    assert calls == [True]


def test_context_menu_display_contents_action_invokes_toggle_display_contents(qapp, monkeypatch):
    node = DocxOperatorNode(str(SAMPLE_DOCX))

    class _FakeMenu(operators.QMenu):
        def exec(self, pos):
            for action in self.actions():
                if action.text() == "Display Contents":
                    return action
            return None

    monkeypatch.setattr(operators, "QMenu", _FakeMenu)

    calls = []
    monkeypatch.setattr(node, "toggle_display_contents", lambda: calls.append(True))

    class _FakeEvent:
        def screenPos(self):
            return None

    node.contextMenuEvent(_FakeEvent())

    assert calls == [True]


def test_context_menu_reload_from_source_action_invokes_reload_from_source(qapp, monkeypatch):
    node = DocxOperatorNode(str(SAMPLE_DOCX))

    class _FakeMenu(operators.QMenu):
        def exec(self, pos):
            for action in self.actions():
                if action.text() == "Reload from Source":
                    return action
            return None

    monkeypatch.setattr(operators, "QMenu", _FakeMenu)

    calls = []
    monkeypatch.setattr(node, "reload_from_source", lambda: calls.append(True))

    class _FakeEvent:
        def screenPos(self):
            return None

    node.contextMenuEvent(_FakeEvent())

    assert calls == [True]


def test_toggle_display_contents_first_call_builds_and_shows_panel(qapp):
    node = DocxOperatorNode(str(SAMPLE_DOCX))
    assert node._content_panel is None

    node.toggle_display_contents()

    assert node._content_panel is not None
    assert node._content_panel.isVisible() is True


def test_toggle_display_contents_second_call_hides_panel(qapp):
    node = DocxOperatorNode(str(SAMPLE_DOCX))

    node.toggle_display_contents()
    node.toggle_display_contents()

    assert node._content_panel.isVisible() is False


def test_toggle_display_contents_refreshes_stale_text_on_reopen(qapp):
    node = DocxOperatorNode(str(SAMPLE_DOCX))
    node.toggle_display_contents()  # show
    node.toggle_display_contents()  # hide

    node.content = "changed while hidden"
    node.toggle_display_contents()  # show again

    assert node._content_editor.toPlainText() == "changed while hidden"


def test_on_widget_text_changed_updates_content_and_marks_dirty(qapp):
    node = DocxOperatorNode(str(SAMPLE_DOCX))
    node.toggle_display_contents()

    node._content_editor.setPlainText("edited by user")

    assert node.content == "edited by user"
    assert node._dirty is True


def test_load_content_dirty_guard_leaves_content_unchanged_without_force(qapp):
    node = DocxOperatorNode(str(SAMPLE_DOCX))
    node._dirty = True
    node.content = "pending edit"

    result = node.load_content()

    assert result == "pending edit"
    assert node.content == "pending edit"
    assert node._dirty is True


def test_load_content_force_true_reloads_and_clears_dirty(qapp):
    node = DocxOperatorNode(str(SAMPLE_DOCX))
    node._dirty = True
    node.content = "pending edit"

    result = node.load_content(force=True)

    assert result == "\n".join(SAMPLE_PARAGRAPHS)
    assert node.content == "\n".join(SAMPLE_PARAGRAPHS)
    assert node._dirty is False


def test_reload_from_source_clears_dirty_and_refreshes_panel(qapp):
    node = DocxOperatorNode(str(SAMPLE_DOCX))
    node.toggle_display_contents()
    node._content_editor.setPlainText("edited by user")
    assert node._dirty is True

    node.reload_from_source()

    assert node._dirty is False
    assert node.content == "\n".join(SAMPLE_PARAGRAPHS)
    assert node._content_editor.toPlainText() == "\n".join(SAMPLE_PARAGRAPHS)


def test_receive_content_updates_content_when_clean(qapp):
    node = DocxOperatorNode(str(SAMPLE_DOCX))

    node.receive_content("new content from flow")

    assert node.content == "new content from flow"


def test_receive_content_is_noop_on_content_when_dirty(qapp):
    node = DocxOperatorNode(str(SAMPLE_DOCX))
    node._dirty = True
    node.content = "pending edit"

    node.receive_content("new content from flow")

    assert node.content == "pending edit"


def test_export_writes_to_existing_export_path_without_prompting(qapp, monkeypatch, tmp_path):
    tmp_txt = tmp_path / "sample_input.txt"
    shutil.copy(SAMPLE_TXT, tmp_txt)
    node = TxtOperatorNode(str(tmp_txt))
    node.content = "content to export"

    def _fail_if_called(*args, **kwargs):
        raise AssertionError("QFileDialog.getSaveFileName should not be called by export()")

    monkeypatch.setattr(operators.QFileDialog, "getSaveFileName", staticmethod(_fail_if_called))

    result = node.export()

    assert result == node.export_path
    assert Path(node.export_path).read_text(encoding="utf-8") == "content to export"


def test_on_widget_text_changed_notifies_every_attached_wire(qapp):
    node = DocxOperatorNode(str(SAMPLE_DOCX))
    node.toggle_display_contents()

    wire_one = Mock()
    wire_two = Mock()
    node.wires.append(wire_one)
    node.wires.append(wire_two)

    node._content_editor.setPlainText("edited by user")

    wire_one.notify_content_edit.assert_called_once_with(node)
    wire_two.notify_content_edit.assert_called_once_with(node)


def test_load_content_refreshes_open_panel_on_real_reload(qapp, tmp_path):
    tmp_txt = tmp_path / "sample_input.txt"
    shutil.copy(SAMPLE_TXT, tmp_txt)
    node = TxtOperatorNode(str(tmp_txt))
    node.toggle_display_contents()

    tmp_txt.write_text("updated on disk", encoding="utf-8")
    node.load_content()  # not dirty -> a real reload happens

    assert node.content == "updated on disk"
    assert node._content_editor.toPlainText() == "updated on disk"


def test_load_content_does_not_refresh_panel_when_dirty_guard_short_circuits(qapp):
    node = DocxOperatorNode(str(SAMPLE_DOCX))
    node.toggle_display_contents()
    node._content_editor.setPlainText("in-progress edit")
    assert node._dirty is True

    node.load_content()  # dirty and not force -> early return, no refresh

    assert node._content_editor.toPlainText() == "in-progress edit"


def test_run_refreshes_open_panel_via_load_content(qapp, tmp_path):
    tmp_txt = tmp_path / "sample_input.txt"
    shutil.copy(SAMPLE_TXT, tmp_txt)
    node = TxtOperatorNode(str(tmp_txt))
    node.toggle_display_contents()

    tmp_txt.write_text("changed before run", encoding="utf-8")
    node.run()  # run() calls load_content(), which now refreshes the panel too

    assert node.content == "changed before run"
    assert node._content_editor.toPlainText() == "changed before run"


def test_export_as_immediately_writes_the_export_file(qapp, monkeypatch, tmp_path):
    tmp_txt = tmp_path / "sample_input.txt"
    shutil.copy(SAMPLE_TXT, tmp_txt)
    node = TxtOperatorNode(str(tmp_txt))
    node.content = "brand new content"
    chosen_path = str(tmp_path / "chosen.txt")

    monkeypatch.setattr(
        operators.QFileDialog, "getSaveFileName", staticmethod(lambda *a, **k: (chosen_path, ""))
    )

    result = node.export_as()

    assert result == chosen_path
    assert Path(chosen_path).exists()
    assert Path(chosen_path).read_text(encoding="utf-8") == "brand new content"
