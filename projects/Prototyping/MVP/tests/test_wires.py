"""Tests for wires.py - headless, real sample/ fixtures.

Verifies content loading via attached nodes, export-path wiring, the
right-click context-menu action shape, and that WireState toggles round-trip
correctly into flow.resolve().
"""

import shutil
import sys
from pathlib import Path

_MVP_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_MVP_ROOT / "File Recompiler"))
sys.path.insert(0, str(_MVP_ROOT / "Joint Interactor"))

from flow import Endpoint
from operators import DocxOperatorNode, TxtOperatorNode
from wires import Wire

SAMPLE_DIR = _MVP_ROOT / "sample"
SAMPLE_DOCX = SAMPLE_DIR / "sample_input.docx"
SAMPLE_TXT = SAMPLE_DIR / "sample_input.txt"


def test_wire_registers_itself_on_both_nodes(qapp):
    node_a = DocxOperatorNode(str(SAMPLE_DOCX))
    node_b = TxtOperatorNode(str(SAMPLE_TXT))

    wire = Wire(node_a, node_b)

    assert wire in node_a.wires
    assert wire in node_b.wires


def test_wire_state_defaults_all_false(qapp):
    node_a = DocxOperatorNode(str(SAMPLE_DOCX))
    node_b = TxtOperatorNode(str(SAMPLE_TXT))

    wire = Wire(node_a, node_b)

    assert wire.state.on is False
    assert wire.state.off is False
    assert wire.state.uni is False
    assert wire.state.bi is False
    assert wire.state.one_shot is False


def test_wire_last_source_node_starts_none(qapp):
    node_a = DocxOperatorNode(str(SAMPLE_DOCX))
    node_b = TxtOperatorNode(str(SAMPLE_TXT))

    wire = Wire(node_a, node_b)

    assert wire.last_source_node is None


def test_wire_toggle_state_feeds_into_flow_resolve_dead_wire(qapp):
    node_a = DocxOperatorNode(str(SAMPLE_DOCX))
    node_b = TxtOperatorNode(str(SAMPLE_TXT))
    wire = Wire(node_a, node_b)

    wire.state.off = True

    plan = wire._current_plan()
    assert plan.shape == "dead-wire"


def test_wire_toggle_state_feeds_into_flow_resolve_live_mirror(qapp):
    node_a = DocxOperatorNode(str(SAMPLE_DOCX))
    node_b = TxtOperatorNode(str(SAMPLE_TXT))
    wire = Wire(node_a, node_b)

    wire.state.bi = True
    wire.state.on = True

    plan = wire._current_plan()
    assert plan.shape == "live-mirror-wire"


def test_trigger_flow_sets_last_source_node_and_returns_plan(qapp, tmp_path):
    tmp_docx = tmp_path / "sample_input.docx"
    tmp_txt = tmp_path / "other.txt"
    shutil.copy(SAMPLE_DOCX, tmp_docx)
    tmp_txt.write_text("hello", encoding="utf-8")

    node_a = DocxOperatorNode(str(tmp_docx))
    node_b = TxtOperatorNode(str(tmp_txt))
    wire = Wire(node_a, node_b)

    plan = wire.trigger_flow(node_a)

    assert wire.last_source_node is node_a
    assert plan.shape == "pulse-wire"


def test_trigger_flow_default_state_writes_export_of_target(qapp, tmp_path):
    tmp_docx = tmp_path / "sample_input.docx"
    tmp_txt = tmp_path / "other.txt"
    shutil.copy(SAMPLE_DOCX, tmp_docx)
    tmp_txt.write_text("hello", encoding="utf-8")

    node_a = DocxOperatorNode(str(tmp_docx))
    node_b = TxtOperatorNode(str(tmp_txt))
    wire = Wire(node_a, node_b)

    wire.trigger_flow(node_a)

    expected_export = tmp_txt.with_name("other.export.txt")
    assert expected_export.exists()
    assert expected_export.read_text(encoding="utf-8") == node_a.content
    # originals stay untouched
    assert tmp_docx.read_bytes() == SAMPLE_DOCX.read_bytes()
    assert tmp_txt.read_text(encoding="utf-8") == "hello"


def test_trigger_flow_default_state_updates_target_node_content(qapp, tmp_path):
    tmp_docx = tmp_path / "sample_input.docx"
    tmp_txt = tmp_path / "other.txt"
    shutil.copy(SAMPLE_DOCX, tmp_docx)
    tmp_txt.write_text("hello", encoding="utf-8")

    node_a = DocxOperatorNode(str(tmp_docx))
    node_b = TxtOperatorNode(str(tmp_txt))
    wire = Wire(node_a, node_b)

    wire.trigger_flow(node_a)

    # not just node_b's export file on disk - its live in-memory .content too
    assert node_b.content == node_a.content


def test_trigger_flow_dead_wire_writes_nothing(qapp, tmp_path):
    tmp_docx = tmp_path / "sample_input.docx"
    tmp_txt = tmp_path / "other.txt"
    shutil.copy(SAMPLE_DOCX, tmp_docx)
    tmp_txt.write_text("original", encoding="utf-8")

    node_a = DocxOperatorNode(str(tmp_docx))
    node_b = TxtOperatorNode(str(tmp_txt))
    wire = Wire(node_a, node_b)
    wire.state.off = True

    wire.trigger_flow(node_a)

    expected_export = tmp_txt.with_name("other.export.txt")
    assert not expected_export.exists()


def test_trigger_flow_arms_live_sync_when_on(qapp, tmp_path):
    tmp_docx = tmp_path / "sample_input.docx"
    tmp_txt = tmp_path / "other.txt"
    shutil.copy(SAMPLE_DOCX, tmp_docx)
    tmp_txt.write_text("hello", encoding="utf-8")

    node_a = DocxOperatorNode(str(tmp_docx))
    node_b = TxtOperatorNode(str(tmp_txt))
    wire = Wire(node_a, node_b)
    wire.state.on = True

    wire.trigger_flow(node_a)

    assert wire.live_sync.watched_path == str(tmp_docx)
    wire.live_sync.disarm()


def test_trigger_flow_once_cadence_does_not_arm_live_sync(qapp, tmp_path):
    tmp_docx = tmp_path / "sample_input.docx"
    tmp_txt = tmp_path / "other.txt"
    shutil.copy(SAMPLE_DOCX, tmp_docx)
    tmp_txt.write_text("hello", encoding="utf-8")

    node_a = DocxOperatorNode(str(tmp_docx))
    node_b = TxtOperatorNode(str(tmp_txt))
    wire = Wire(node_a, node_b)

    wire.trigger_flow(node_a)  # default state -> ONCE cadence

    assert wire.live_sync.watched_path is None


def test_detach_removes_wire_from_both_nodes(qapp):
    node_a = DocxOperatorNode(str(SAMPLE_DOCX))
    node_b = TxtOperatorNode(str(SAMPLE_TXT))
    wire = Wire(node_a, node_b)

    wire.detach()

    assert wire not in node_a.wires
    assert wire not in node_b.wires


def test_other_node_returns_the_opposite_endpoint(qapp):
    node_a = DocxOperatorNode(str(SAMPLE_DOCX))
    node_b = TxtOperatorNode(str(SAMPLE_TXT))
    wire = Wire(node_a, node_b)

    assert wire.other_node(node_a) is node_b
    assert wire.other_node(node_b) is node_a


# -- notify_content_edit() (real-time panel mirroring) ----------------------


def test_notify_content_edit_is_noop_with_no_designated_source(qapp, tmp_path):
    tmp_docx = tmp_path / "sample_input.docx"
    tmp_txt = tmp_path / "other.txt"
    shutil.copy(SAMPLE_DOCX, tmp_docx)
    tmp_txt.write_text("hello", encoding="utf-8")

    node_a = DocxOperatorNode(str(tmp_docx))
    node_b = TxtOperatorNode(str(tmp_txt))
    wire = Wire(node_a, node_b)
    wire.state.bi = True
    wire.state.on = True
    # last_source_node is still None - Run has never been used on this wire.

    node_a.content = "live edit"
    wire.notify_content_edit(node_a)

    expected_export = tmp_txt.with_name("other.export.txt")
    assert not expected_export.exists()
    assert node_b.content == "hello"


def test_notify_content_edit_is_noop_for_off_wire_even_after_run(qapp, tmp_path):
    tmp_docx = tmp_path / "sample_input.docx"
    tmp_txt = tmp_path / "other.txt"
    shutil.copy(SAMPLE_DOCX, tmp_docx)
    tmp_txt.write_text("hello", encoding="utf-8")

    node_a = DocxOperatorNode(str(tmp_docx))
    node_b = TxtOperatorNode(str(tmp_txt))
    wire = Wire(node_a, node_b)
    wire.state.on = True
    wire.trigger_flow(node_a)  # designates node_a as source, arms live sync
    wire.state.off = True  # Off always wins, regardless of On

    content_after_run = node_b.content
    node_a.content = "live edit"
    wire.notify_content_edit(node_a)

    assert node_b.content == content_after_run
    assert node_b.content != "live edit"


def test_notify_content_edit_is_noop_for_plain_pulse_wire_after_run(qapp, tmp_path):
    tmp_docx = tmp_path / "sample_input.docx"
    tmp_txt = tmp_path / "other.txt"
    shutil.copy(SAMPLE_DOCX, tmp_docx)
    tmp_txt.write_text("hello", encoding="utf-8")

    node_a = DocxOperatorNode(str(tmp_docx))
    node_b = TxtOperatorNode(str(tmp_txt))
    wire = Wire(node_a, node_b)
    wire.trigger_flow(node_a)  # default state -> pulse-wire, source designated

    assert wire.last_source_node is node_a
    assert wire._current_plan().watch is False

    content_after_run = node_b.content
    node_a.content = "live edit"
    wire.notify_content_edit(node_a)

    # a plain (non-watch) pulse wire only fires on an explicit Run, never on
    # every keystroke.
    assert node_b.content == content_after_run
    assert node_b.content != "live edit"


def test_notify_content_edit_is_noop_for_one_shot_wire_after_run(qapp, tmp_path):
    tmp_docx = tmp_path / "sample_input.docx"
    tmp_txt = tmp_path / "other.txt"
    shutil.copy(SAMPLE_DOCX, tmp_docx)
    tmp_txt.write_text("hello", encoding="utf-8")

    node_a = DocxOperatorNode(str(tmp_docx))
    node_b = TxtOperatorNode(str(tmp_txt))
    wire = Wire(node_a, node_b)
    wire.state.one_shot = True
    wire.trigger_flow(node_a)

    assert wire.last_source_node is node_a
    assert wire._current_plan().watch is False

    content_after_run = node_b.content
    node_a.content = "live edit"
    wire.notify_content_edit(node_a)

    assert node_b.content == content_after_run
    assert node_b.content != "live edit"


def test_notify_content_edit_uni_on_wire_propagates_source_to_target_only(qapp, tmp_path):
    tmp_docx = tmp_path / "sample_input.docx"
    tmp_txt = tmp_path / "other.txt"
    shutil.copy(SAMPLE_DOCX, tmp_docx)
    tmp_txt.write_text("hello", encoding="utf-8")

    node_a = DocxOperatorNode(str(tmp_docx))
    node_b = TxtOperatorNode(str(tmp_txt))
    wire = Wire(node_a, node_b)
    wire.state.uni = True
    wire.state.on = True
    wire.trigger_flow(node_a)  # node_a designated as source, live sync armed

    assert wire._current_plan().watch is True

    # source -> target: editing node_a (the designated source) propagates to
    # node_b. Mirrors _on_widget_text_changed(), which sets both content and
    # _dirty before notifying wires - _dirty must be set here too, otherwise
    # flow.execute()'s from_node.load_content() call would re-read node_a's
    # content straight back off disk before propagating it.
    node_a.content = "edited on source"
    node_a._dirty = True
    wire.notify_content_edit(node_a)

    assert node_b.content == "edited on source"
    expected_export = tmp_txt.with_name("other.export.txt")
    assert expected_export.read_text(encoding="utf-8") == "edited on source"

    # target -> source: editing node_b (not the designated source) does NOT
    # propagate back onto node_a for a Uni wire.
    node_b.content = "edited on target"
    node_b._dirty = True
    wire.notify_content_edit(node_b)

    assert node_a.content == "edited on source"


def test_notify_content_edit_bi_on_wire_propagates_both_ways(qapp, tmp_path):
    tmp_docx = tmp_path / "sample_input.docx"
    tmp_txt = tmp_path / "other.txt"
    shutil.copy(SAMPLE_DOCX, tmp_docx)
    tmp_txt.write_text("hello", encoding="utf-8")

    node_a = DocxOperatorNode(str(tmp_docx))
    node_b = TxtOperatorNode(str(tmp_txt))
    wire = Wire(node_a, node_b)
    wire.state.bi = True
    wire.state.on = True
    wire.trigger_flow(node_a)  # node_a designated as source, live sync armed

    assert wire._current_plan().watch is True
    assert wire._current_plan().shape == "live-mirror-wire"

    # B -> A first, while both nodes start clean (trigger_flow above already
    # cleared dirty on both via the two legs' own from_node.load_content()
    # calls). Bi means either endpoint's edit propagates to the other, even
    # though node_a (not node_b) is the designated last_source_node.
    node_b.content = "edit from b"
    node_b._dirty = True  # mirrors _on_widget_text_changed() setting content+_dirty together
    wire.notify_content_edit(node_b)
    assert node_a.content == "edit from b"

    # A -> B - reset node_b's dirty flag first (it isn't "still mid-edit" in
    # this second scenario), so its own receive_content() guard doesn't
    # itself block the incoming mirror from node_a.
    node_b._dirty = False
    node_a.content = "edit from a"
    node_a._dirty = True
    wire.notify_content_edit(node_a)
    assert node_b.content == "edit from a"


def test_notify_content_edit_respects_target_dirty_guard_but_still_writes_export(qapp, tmp_path):
    tmp_docx = tmp_path / "sample_input.docx"
    tmp_txt = tmp_path / "other.txt"
    shutil.copy(SAMPLE_DOCX, tmp_docx)
    tmp_txt.write_text("hello", encoding="utf-8")

    node_a = DocxOperatorNode(str(tmp_docx))
    node_b = TxtOperatorNode(str(tmp_txt))
    wire = Wire(node_a, node_b)
    wire.state.on = True
    wire.trigger_flow(node_a)

    # simulate node_b having an in-progress edit of its own (dirty) -
    # receive_content() should skip updating node_b's buffer/display, but
    # flow.execute() still writes node_b's export file regardless.
    node_b._dirty = True
    node_b.content = "node_b's own pending edit"

    # node_a._dirty must be set too, mirroring _on_widget_text_changed(),
    # otherwise flow.execute()'s from_node.load_content() call would re-read
    # node_a's content straight back off disk before propagating it.
    node_a.content = "edit from a"
    node_a._dirty = True
    wire.notify_content_edit(node_a)

    assert node_b.content == "node_b's own pending edit"  # buffer not clobbered
    expected_export = tmp_txt.with_name("other.export.txt")
    assert expected_export.read_text(encoding="utf-8") == "edit from a"  # export still written

