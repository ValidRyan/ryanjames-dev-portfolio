"""Headless end-to-end smoke tests for "Feature addition 2: true real-time
panel mirroring" (Joint Interactor).

Real `TxtOperatorNode`/`DocxOperatorNode` instances against real
`Prototyping/MVP/sample/` fixtures (copied into `tmp_path` first - never
touching the checked-in sample/ files directly), a real `Wire`, and a real
`QPlainTextEdit` embedded editor per node. Edits are made via the editor's
own API (`setPlainText`/`insertPlainText`), NOT simulated keystrokes,
matching this suite's existing convention (see test_operators.py's Display
Contents tests). Assertions check the *other* node's editor's
`toPlainText()` reflects the edit immediately - synchronously, no
wait/sleep, no disk round-trip.
"""

import shutil
import sys
from pathlib import Path

_MVP_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_MVP_ROOT / "File Recompiler"))
sys.path.insert(0, str(_MVP_ROOT / "Joint Interactor"))

from operators import DocxOperatorNode, TxtOperatorNode
from wires import Wire

SAMPLE_DIR = _MVP_ROOT / "sample"
SAMPLE_DOCX = SAMPLE_DIR / "sample_input.docx"
SAMPLE_TXT = SAMPLE_DIR / "sample_input.txt"


def _make_pair(tmp_path):
    """Real DocxOperatorNode + TxtOperatorNode pair against tmp_path copies
    of the real sample/ fixtures (sample/ itself is never touched)."""
    tmp_docx = tmp_path / "sample_input.docx"
    tmp_txt = tmp_path / "other.txt"
    shutil.copy(SAMPLE_DOCX, tmp_docx)
    tmp_txt.write_text("hello", encoding="utf-8")

    node_a = DocxOperatorNode(str(tmp_docx))
    node_b = TxtOperatorNode(str(tmp_txt))
    return node_a, node_b


def _make_bi_on_wire(node_a, node_b):
    wire = Wire(node_a, node_b)
    wire.state.bi = True
    wire.state.on = True

    # Both panels must be open before editing their embedded editors -
    # matches test_operators.py's Display Contents panel test setup.
    node_a.toggle_display_contents()
    node_b.toggle_display_contents()

    # Establish node_a as the designated source and arm live sync (Run).
    wire.trigger_flow(node_a)
    assert wire.last_source_node is node_a
    assert wire._current_plan().watch is True
    return wire


def test_bi_on_wire_mirrors_live_edits_a_to_b(qapp, tmp_path):
    node_a, node_b = _make_pair(tmp_path)
    _make_bi_on_wire(node_a, node_b)

    # Editing node_a's embedded editor directly (its own API, not simulated
    # keystrokes) should immediately mirror onto node_b's editor - no
    # wait/sleep, no disk round-trip.
    node_a._content_editor.setPlainText("typed live into A")
    assert node_b._content_editor.toPlainText() == "typed live into A"


def test_bi_on_wire_mirrors_live_edits_b_to_a(qapp, tmp_path):
    # A fresh node pair, deliberately separate from the a->b test above:
    # once a node has itself been live-edited it stays dirty (by pre-existing
    # design - only an explicit force-reload clears it), and receive_content()
    # correctly refuses to clobber a dirty node's buffer. Reusing the same
    # pair for both directions in sequence would exercise that unrelated
    # dirty-guard interaction instead of the bidirectional-mirroring property
    # this test is actually about, so each direction gets its own pair.
    node_a, node_b = _make_pair(tmp_path)
    _make_bi_on_wire(node_a, node_b)

    # And the reverse, since this is a Bi wire - editing node_b mirrors onto
    # node_a's editor immediately.
    node_b._content_editor.setPlainText("typed live into B")
    assert node_a._content_editor.toPlainText() == "typed live into B"


def test_uni_on_wire_mirrors_only_from_the_designated_source(qapp, tmp_path):
    node_a, node_b = _make_pair(tmp_path)
    wire = Wire(node_a, node_b)
    wire.state.uni = True
    wire.state.on = True

    node_a.toggle_display_contents()
    node_b.toggle_display_contents()

    wire.trigger_flow(node_a)
    assert wire.last_source_node is node_a
    assert wire._current_plan().watch is True

    # source -> target mirrors live.
    node_a._content_editor.setPlainText("source edit")
    assert node_b._content_editor.toPlainText() == "source edit"

    # target -> source must NOT mirror back for a Uni wire.
    node_b._content_editor.setPlainText("target edit, should not propagate")
    assert node_a._content_editor.toPlainText() == "source edit"
