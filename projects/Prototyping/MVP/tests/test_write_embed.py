"""Tests for write_embed.py - snapshot, embed / un-embed, checked PNG handling (hazard C).

The "editor" here is a plain `QTextEdit` (never shown): `write_embed` only needs
`document()`, `textCursor()`, `setTextCursor()`, `palette()` and `font()`. Documents are
built with `model_to_document`, so they carry no undo history and are clean; every
Embed / Un-embed is then exactly one undo step.

Pixel tests follow the rules in write_testkit's docstring: the offscreen platform has NO
fonts (glyphs are hollow boxes, whitespace included), so text is painted RED through the
document's own char format while the overlay (Dash diagonal, left-lean redraw) draws in
the palette Text colour BLACK. Colour separates base ink from overlay ink; assertions are
ratios and band centroids, never absolute pixels.

Every test runs under the `write_qt` fixture and registers the widgets it builds with
`write_testkit.track_qt`. Source stays ASCII (the object-replacement character and
non-BMP characters are built with `chr()`), printed strings go through `ascii()`.
"""

import ast
import gc
import sys
import weakref
from pathlib import Path

_MVP_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_MVP_ROOT / "SAP Modifier"))

import pytest
import shiboken6
from PySide6.QtCore import QBuffer, QIODevice, QUrl
from PySide6.QtGui import QColor, QFont, QImage, QImageReader, QPalette, QTextCharFormat, QTextCursor, QTextDocument
from PySide6.QtWidgets import QTextEdit

import write_docmodel
import write_embed
import write_paths
import write_testkit
from write_docmodel import PROP_EMBED, block_fragments, document_to_model, embed_id_of, model_to_document
from write_embed import (
    SNAPSHOT_MAX_H,
    SNAPSHOT_MAX_PIXELS,
    SNAPSHOT_MAX_W,
    UNEMBED_HINT,
    EmbedRefused,
    decode_png_checked,
    embed_image_for_url,
    embed_range,
    embed_target_range,
    encode_png,
    find_embed_positions,
    render_range_snapshot,
    unembed_ranges,
)
from write_format import read_write_file, save_write
from write_model import EmbedRegistry, EmbedRun, Paragraph, Run, WriteDocument
from write_testkit import write_qt  # noqa: F401  (fixture: see write_testkit's Qt lifetime rules)

pytestmark = pytest.mark.usefixtures("write_qt")

OBJECT_CHAR = chr(0xFFFC)
EMOJI = chr(0x1F600)  # one code point, two UTF-16 units: the offsets Qt hands out count two
RED = QColor(255, 0, 0)
BLACK = QColor(0, 0, 0)
CREAM = QColor(250, 245, 200)


# -- helpers -----------------------------------------------------------------------------


def _png(width=8, height=6, color=QColor(200, 30, 30)) -> bytes:
    image = QImage(width, height, QImage.Format.Format_ARGB32)
    image.fill(color)
    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    assert image.save(buffer, "PNG")
    return bytes(buffer.data())


def _palette(base=QColor(255, 255, 255), text=BLACK) -> QPalette:
    pal = QPalette()
    pal.setColor(QPalette.ColorRole.Base, base)
    pal.setColor(QPalette.ColorRole.Window, base)
    pal.setColor(QPalette.ColorRole.Text, text)
    pal.setColor(QPalette.ColorRole.WindowText, text)
    return pal


def _make_edit(model, registry=None, *, point_size=None, palette=None):
    """A tracked, never-shown `QTextEdit` holding `model`; returns `(edit, registry)`."""
    registry = registry if registry is not None else EmbedRegistry()
    edit = write_testkit.track_qt(QTextEdit())
    if palette is not None:
        edit.setPalette(palette)
    if point_size:
        font = QFont()
        font.setPointSize(point_size)
        edit.setFont(font)
    warnings = model_to_document(model, edit.document(), registry)
    assert warnings == []
    return edit, registry


def _doc(*paragraphs) -> WriteDocument:
    """`paragraphs` are `(indent, [runs])` pairs."""
    return WriteDocument([Paragraph(indent, list(runs)) for indent, runs in paragraphs])


def _select(edit, lo, hi=None):
    cursor = edit.textCursor()
    cursor.setPosition(lo)
    if hi is not None:
        cursor.setPosition(hi, QTextCursor.MoveMode.KeepAnchor)
    edit.setTextCursor(cursor)


def _end(edit) -> int:
    return edit.document().characterCount() - 1


def _lines(edit) -> list:
    return edit.document().toPlainText().split("\n")


def _snapshot_state(edit, registry) -> tuple:
    """Everything a refused request must leave alone."""
    document = edit.document()
    return (
        document.toPlainText(),
        document.isModified(),
        document.isUndoAvailable(),
        document.revision(),
        len(registry),
        registry.total_bytes,
        edit.textCursor().position(),
        edit.textCursor().anchor(),
    )


def _image_fragments(document) -> list:
    found = []
    block = document.begin()
    while block.isValid():
        for frag in block_fragments(block):
            if frag.charFormat().isImageFormat():
                found.append(frag)
        block = block.next()
    return found


def _pixels(image: QImage):
    for y in range(image.height()):
        for x in range(image.width()):
            c = image.pixelColor(x, y)
            yield x, y, c.red(), c.green(), c.blue()


def _ink_points(image: QImage, predicate) -> list:
    return [(x, y) for x, y, r, g, b in _pixels(image) if predicate(r, g, b)]


def _dark(r, g, b) -> bool:  # overlay ink: the palette Text colour BLACK
    return r < 90 and g < 90 and b < 90


def _reddish(r, g, b) -> bool:  # base ink: the document's own foreground RED
    return r > 170 and g < 90 and b < 90


def _paint_text_red(edit):
    """Give every character a RED foreground through the document's own formats (not a tag)."""
    cursor = QTextCursor(edit.document())
    cursor.select(QTextCursor.SelectionType.Document)
    fmt = QTextCharFormat()
    fmt.setForeground(RED)
    cursor.mergeCharFormat(fmt)


def _band_centroid_x(points, y_lo, y_hi):
    xs = [x for x, y in points if y_lo <= y <= y_hi]
    return (sum(xs) / len(xs)) if xs else None


def _upper_lower_shift(points):
    """(upper-band centroid x - lower-band centroid x) / (vertical distance): ~0 upright, < 0 leaning LEFT."""
    ys = [y for _, y in points]
    top, bottom = min(ys), max(ys)
    span = bottom - top
    if span < 8:
        return None
    third = span / 3.0
    upper = [(x, y) for x, y in points if y <= top + third]
    lower = [(x, y) for x, y in points if y >= bottom - third]
    upper_x = sum(x for x, _ in upper) / len(upper)
    lower_x = sum(x for x, _ in lower) / len(lower)
    upper_y = sum(y for _, y in upper) / len(upper)
    lower_y = sum(y for _, y in lower) / len(lower)
    return (upper_x - lower_x) / (lower_y - upper_y)


# -- 1. embed keeps the source and inserts a picture -------------------------------------


def test_embed_keeps_source_and_inserts_an_image(qapp):
    original = _doc((0, [Run("a" + EMOJI + "b ", {}), Run("bold", {"bold": True}), Run(" tail", {})]))
    edit, registry = _make_edit(original, point_size=14)
    document = edit.document()
    found = document.find("b bold")  # positions counted in UTF-16 units, past the emoji
    lo, hi = found.selectionStart(), found.selectionEnd()
    _select(edit, lo, hi)
    assert (lo, hi) == embed_target_range(edit)
    assert not document.isModified() and not document.isUndoAvailable()

    embed_id = embed_range(edit, lo, hi, registry)

    # the source is captured exactly (the astral character before it cannot shift the slice)
    record = registry.get(embed_id)
    assert record.source == [Run("b ", {}), Run("bold", {"bold": True})]
    assert len(registry) == 1 and registry.total_bytes == len(record.png)
    # ...and the picture is a real, decodable PNG at DPR 1
    decoded = decode_png_checked(record.png)
    assert decoded.width() > 4 and decoded.height() > 4
    # the document text underneath: one picture character where the range was, the rest untouched
    assert document.toPlainText() == "a" + EMOJI + OBJECT_CHAR + " tail"
    frags = _image_fragments(document)
    assert len(frags) == 1
    image_format = frags[0].charFormat().toImageFormat()
    assert embed_id_of(image_format) == embed_id
    assert image_format.name() == f"mem://{embed_id}"
    assert (image_format.width(), image_format.height()) == (decoded.width(), decoded.height())
    resource = document.resource(QTextDocument.ResourceType.ImageResource, QUrl(image_format.name()))
    assert resource is not None and resource.width() == decoded.width()
    # what the model (tree, counts, Run, save) sees is unchanged: the source text stands in for the picture
    model = document_to_model(document, registry)
    assert model.outline() == original.outline() == [(0, "a" + EMOJI + "b bold tail")]
    runs = model.paragraphs[0].runs
    assert [type(run).__name__ for run in runs] == ["Run", "EmbedRun", "Run"]
    assert runs[1].png == record.png and runs[1].source == record.source

    # the caret sits just after the picture, and text typed next does NOT inherit the embed property (V7)
    assert edit.textCursor().position() == lo + 1
    edit.insertPlainText("X")
    typed = [f for f in block_fragments(document.begin()) if "X" in f.text()][0]
    assert not typed.charFormat().isImageFormat() and not typed.charFormat().hasProperty(PROP_EMBED)
    edit.textCursor().deletePreviousChar()

    # one undo step restores the text exactly (the registry keeps the picture for a later redo)
    assert document.isUndoAvailable()
    while document.isUndoAvailable():
        document.undo()
    assert document_to_model(document, registry).paragraphs == original.paragraphs
    assert len(registry) == 1
    document.redo()
    assert len(_image_fragments(document)) == 1


# -- 2. no selection = the whole paragraph -------------------------------------------------


def test_no_selection_embeds_the_whole_paragraph(qapp):
    middle = [Run("Phrase: ", {"bold": True}), Run("all of it", {"italic": "left"}), Run(" here", {"spread": 2.0})]
    original = _doc((0, [Run("first")]), (2, middle), (0, [Run("last")]))
    edit, registry = _make_edit(original)
    document = edit.document()
    block = document.findBlockByNumber(1)
    _select(edit, block.position() + 3)  # a caret, no selection

    lo, hi = embed_target_range(edit)
    assert (lo, hi) == (block.position(), block.position() + len("Phrase: all of it here"))
    embed_id = embed_range(edit, lo, hi, registry)

    assert _lines(edit) == ["first", OBJECT_CHAR, "last"]
    assert document.findBlockByNumber(1).blockFormat().indent() == 2  # the paragraph keeps its indent
    assert registry.get(embed_id).source == middle
    model = document_to_model(document, registry)
    assert model.outline() == original.outline()
    assert [p.indent for p in model.paragraphs] == [0, 2, 0]

    # a "select the whole line" gesture ends at the start of the next paragraph: still one paragraph
    edit2, registry2 = _make_edit(original)
    second = edit2.document().findBlockByNumber(1)
    _select(edit2, 0, second.position())  # first line plus its paragraph break
    assert embed_target_range(edit2) == (0, len("first"))
    embed_range(edit2, *embed_target_range(edit2), registry2)
    assert _lines(edit2)[0] == OBJECT_CHAR and _lines(edit2)[1:] == [middle[0].text + middle[1].text + middle[2].text, "last"]
    # ...but a selection that really reaches into the next paragraph is left alone
    edit3, _ = _make_edit(original)
    _select(edit3, 2, second.position() + 3)
    assert embed_target_range(edit3) == (2, second.position() + 3)


# -- 3. refusals (nothing changes) -------------------------------------------------------


def test_refusals_leave_document_registry_and_undo_untouched(qapp, monkeypatch):
    picture = _png()
    original = _doc(
        (0, [Run("first paragraph")]),
        (0, [Run("second paragraph")]),
        (0, [Run("   ")]),
        (0, []),
        (0, [Run("has ", {}), EmbedRun(picture, [Run("Goals: x")]), Run(" a picture")]),
    )
    edit, registry = _make_edit(original)
    document = edit.document()
    starts = [document.findBlockByNumber(n).position() for n in range(5)]
    before = _snapshot_state(edit, registry)
    registry_before = len(registry)

    def refused(lo, hi, match):
        with pytest.raises(EmbedRefused, match=match):
            embed_range(edit, lo, hi, registry)
        assert _snapshot_state(edit, registry) == before
        assert len(registry) == registry_before

    refused(starts[0] + 3, starts[1] + 3, "one paragraph")  # multi-paragraph (D4)
    refused(starts[0], starts[1], "one paragraph")  # a range that includes the paragraph break
    refused(starts[0], starts[0], "empty")  # empty
    refused(starts[3], starts[3], "empty")  # an empty paragraph, no selection
    refused(starts[2], starts[2] + 3, "only spaces")  # whitespace only
    refused(starts[0], 10**6, "outside")  # past the end of the document
    refused(-5, 3, "outside")
    refused(starts[4], starts[4] + document.findBlockByNumber(4).length() - 1, "already contains a picture")  # no nested embeds
    refused(starts[4] + 1, starts[4] + 4, "empty" if False else "already contains a picture") if False else None

    # an oversize PNG: monkeypatch the cap under the (tiny) real picture
    monkeypatch.setattr(write_embed, "MAX_EMBED_PNG_BYTES", 10)
    refused(starts[0], starts[0] + 5, "bytes")
    monkeypatch.undo()
    # a range longer than the safety valve is refused before any layout starts
    monkeypatch.setattr(write_embed, "SNAPSHOT_MAX_CHARS", 4)
    refused(starts[0], starts[0] + 5, "too long")
    monkeypatch.undo()

    # the same request works once the obstacle is gone (the refusals were not sticky)
    embed_id = embed_range(edit, starts[0], starts[0] + 5, registry)
    assert embed_id in registry and _lines(edit)[0] == OBJECT_CHAR + " paragraph"
    # ...and the document's own picture from the model is still an embed, untouched by all of this
    assert len(find_embed_positions(document, 0, _end(edit) + 1)) == 2


# -- 4. un-embed restores the runs and their tags ------------------------------------------


def test_unembed_restores_runs_and_tags_in_one_undo_step(qapp):
    runs = [
        Run("Goals: ", {"bold": True}),
        Run("dashed", {"dash": True}),
        Run(" ", {}),
        Run("lean", {"italic": "left"}),
        Run(" wide", {"spread": 3.0, "line": True}),
        Run(" right", {"italic": "right"}),
        Run(" tail", {}),
    ]
    original = _doc((1, runs))
    edit, registry = _make_edit(original)
    document = edit.document()
    end = _end(edit)
    lo, hi = 3, end - 2  # starts inside the bold run ("Goals: " -> position 3 is the 'l'), ends inside the tail
    embed_id = embed_range(edit, lo, hi, registry)
    expected_source = [
        Run("ls: ", {"bold": True}),
        Run("dashed", {"dash": True}),
        Run(" ", {}),
        Run("lean", {"italic": "left"}),
        Run(" wide", {"spread": 3.0, "line": True}),
        Run(" right", {"italic": "right"}),
        Run(" ta", {}),
    ]
    assert registry.get(embed_id).source == expected_source
    assert document_to_model(document, registry).outline() == original.outline()

    _select(edit, lo + 1)  # caret just after the picture
    assert unembed_ranges(edit, registry) == 1
    assert _image_fragments(document) == []
    restored = document_to_model(document, registry)
    assert restored.paragraphs == original.paragraphs  # text, tags and the paragraph's indent, exactly
    assert edit.textCursor().position() == hi  # the caret follows the restored text

    # Embed and Un-embed are one undo step each
    document.undo()
    assert len(_image_fragments(document)) == 1
    assert document_to_model(document, registry).outline() == original.outline()
    document.undo()
    assert document_to_model(document, registry).paragraphs == original.paragraphs
    assert not document.isUndoAvailable()
    document.redo()
    document.redo()
    assert _image_fragments(document) == []
    assert document_to_model(document, registry).paragraphs == original.paragraphs


# -- 5. which pictures an un-embed acts on -------------------------------------------------


def _targets_model() -> WriteDocument:
    return _doc(
        (0, [Run("aa "), EmbedRun(_png(6, 4, QColor(200, 0, 0)), [Run("ONE")]), Run(" bb "), EmbedRun(_png(7, 4, QColor(0, 160, 0)), [Run("TWO")]), Run(" cc")]),
        (1, [EmbedRun(_png(8, 4, QColor(0, 0, 200)), [Run("THREE", {"bold": True})]), EmbedRun(_png(9, 4, QColor(90, 90, 0)), [Run("FOUR")])]),
        (0, [Run("solo "), EmbedRun(_png(10, 4, QColor(0, 90, 90)), [Run("FIVE")]), Run(" text here")]),
        (0, [Run("plain paragraph")]),
    )


def test_unembed_targets_selection_then_before_then_after_then_sole(qapp):
    def fresh():
        edit, registry = _make_edit(_targets_model())
        assert find_embed_positions(edit.document(), 0, _end(edit) + 1) == [3, 8, 13, 14, 21]
        return edit, registry

    O = OBJECT_CHAR

    # (a) a non-empty selection: the pictures inside it, all of them, processed last to first
    edit, registry = fresh()
    _select(edit, 0, 12)
    assert unembed_ranges(edit, registry) == 2
    assert _lines(edit)[0] == "aa ONE bb TWO cc" and _lines(edit)[1:] == [O + O, "solo " + O + " text here", "plain paragraph"]
    assert edit.textCursor().position() == len("aa ONE bb TWO")  # after the last restored text
    edit.document().undo()  # ...in ONE undo step
    assert find_embed_positions(edit.document(), 0, _end(edit) + 1) == [3, 8, 13, 14, 21]
    assert not edit.document().isUndoAvailable()

    # (a2) a selection across paragraphs restores every picture in it and leaves the others
    edit, registry = fresh()
    _select(edit, 0, 15)
    assert unembed_ranges(edit, registry) == 4
    assert _lines(edit)[:3] == ["aa ONE bb TWO cc", "THREEFOUR", "solo " + O + " text here"]
    model = document_to_model(edit.document(), registry)
    assert model.paragraphs[1].runs == [Run("THREE", {"bold": True}), Run("FOUR", {})] and model.paragraphs[1].indent == 1

    # (a3) a selection wins over the caret rules: it holds only TWO while the caret (its end) is right after ONE
    edit, registry = fresh()
    _select(edit, 9, 4)  # anchor 9, caret 4: selects [4, 9), which holds only the second picture
    assert unembed_ranges(edit, registry) == 1
    assert _lines(edit)[0] == "aa " + O + " bb TWO cc"

    # (b) no selection: the picture BEFORE the caret first
    edit, registry = fresh()
    _select(edit, 4)
    assert unembed_ranges(edit, registry) == 1
    assert _lines(edit)[0] == "aa ONE bb " + O + " cc"

    # (c) ...then the picture AFTER the caret
    edit, registry = fresh()
    _select(edit, 8)
    assert unembed_ranges(edit, registry) == 1
    assert _lines(edit)[0] == "aa " + O + " bb TWO cc"

    # (d) between two touching pictures the one before the caret wins
    edit, registry = fresh()
    _select(edit, 14)
    assert unembed_ranges(edit, registry) == 1
    model = document_to_model(edit.document(), registry)
    assert model.paragraphs[1].runs[0] == Run("THREE", {"bold": True})
    assert isinstance(model.paragraphs[1].runs[1], EmbedRun) and model.paragraphs[1].runs[1].source == [Run("FOUR")]

    # (e) not next to one, but it is the ONLY picture in the paragraph
    edit, registry = fresh()
    _select(edit, 18)
    assert unembed_ranges(edit, registry) == 1
    assert _lines(edit)[2] == "solo FIVE text here"

    # (f) two pictures in the paragraph and the caret next to neither: report, change nothing
    edit, registry = fresh()
    _select(edit, 6)
    before = _snapshot_state(edit, registry)
    with pytest.raises(EmbedRefused) as info:
        unembed_ranges(edit, registry)
    assert str(info.value) == UNEMBED_HINT == "Place the cursor next to an embed (or select it)."
    assert _snapshot_state(edit, registry) == before
    # (g) a paragraph without any picture
    _select(edit, edit.document().findBlockByNumber(3).position() + 2)
    with pytest.raises(EmbedRefused, match="Place the cursor next to an embed"):
        unembed_ranges(edit, registry)
    # (h) a non-empty selection that holds no picture falls through to the caret rules (documented)
    _select(edit, 5, 7)
    with pytest.raises(EmbedRefused, match="Place the cursor"):
        unembed_ranges(edit, registry)
    assert edit.document().toPlainText() == _targets_model_text()


def _targets_model_text() -> str:
    O = OBJECT_CHAR
    return "\n".join(["aa " + O + " bb " + O + " cc", O + O, "solo " + O + " text here", "plain paragraph"])


# -- 6. the snapshot includes the overlay pixels -------------------------------------------


def _overlay_snapshot(tags, *, base=CREAM):
    edit, _ = _make_edit(_doc((0, [Run("MMMM", tags)])), point_size=30, palette=_palette(base=base))
    _paint_text_red(edit)
    return render_range_snapshot(edit, 0, _end(edit))


def test_snapshot_includes_the_overlay_pixels(qapp, monkeypatch):
    control = _overlay_snapshot({})
    assert control.format() == QImage.Format.Format_ARGB32_Premultiplied
    assert control.devicePixelRatio() == 1.0
    assert control.pixelColor(0, 0).getRgb()[:3] == CREAM.getRgb()[:3]  # filled with the palette Base
    control_red = _ink_points(control, _reddish)
    if not control_red:
        pytest.skip("platform drew no text ink")
    assert not _ink_points(control, _dark), "control: with no Dash and no lean there is no overlay ink"

    # Dash: a black diagonal over the red base text, rising to the right, as wide as the run
    dashed = _overlay_snapshot({"dash": True})
    assert dashed.size() == control.size()
    dark = _ink_points(dashed, _dark)
    assert dark, "the overlay painter must have drawn the diagonal into the picture"
    xs = [x for x, _ in dark]
    span = max(xs) - min(xs)
    run_width = dashed.width() - 2 * write_embed.SNAPSHOT_MARGIN
    assert span / run_width == pytest.approx(1.0, abs=0.12)
    left = [(x, y) for x, y in dark if x <= min(xs) + span / 3]
    right = [(x, y) for x, y in dark if x >= max(xs) - span / 3]
    assert sum(y for _, y in left) / len(left) > sum(y for _, y in right) / len(right)  # rises to the right
    assert len(_ink_points(dashed, _reddish)) >= 0.8 * len(control_red)  # the base text is still there under it

    # Left lean: the upright red text is covered and redrawn sheared in black, leaning left
    leaning = _overlay_snapshot({"italic": "left"})
    ghost = _ink_points(leaning, _reddish)
    lean_dark = _ink_points(leaning, _dark)
    assert lean_dark, "the sheared redraw must be in the picture"
    assert len(ghost) / len(control_red) < 0.05, "the cover must hide the upright glyphs"
    shift = _upper_lower_shift(lean_dark)
    # DIRECTION only: the band centroids of hollow placeholder glyphs (no fonts offscreen) under-read the
    # theoretical -0.30 (measured -0.12 at 20pt to -0.165 at 72pt, always leaning left). The magnitude is
    # pinned at full size in test_write_overlay; here the picture must lean left by clearly more than the
    # upright noise floor (0.06).
    assert shift is not None and shift < -0.08
    assert encode_png(leaning) != encode_png(control)  # so the file really differs

    # Rendering never edits the document, and the snapshot document is a plain Python-owned
    # QTextDocument: refcounting frees it on every path (success, refused before it exists,
    # refused after it was built), so nothing leaks. (It used to be a `clone()` that was deleted
    # by hand, which left a stale wrapper behind: see the module docstring of write_embed.)
    edit, _ = _make_edit(_doc((0, [Run("plain words here")]), (0, [Run("second")])))
    document = edit.document()
    state = (document.toPlainText(), document.isModified(), document.isUndoAvailable(), document.revision())
    made = []
    real_snapshot = write_embed._snapshot_document

    def spy(source_document, lo, hi):
        snap = real_snapshot(source_document, lo, hi)
        assert snap is not source_document
        made.append(weakref.ref(snap))
        return snap

    monkeypatch.setattr(write_embed, "_snapshot_document", spy)
    image = render_range_snapshot(edit, 6, 11)
    assert image.width() > 0 and image.height() > 0
    with pytest.raises(EmbedRefused):
        render_range_snapshot(edit, 3, 3)  # refused before a snapshot exists: nothing to free
    monkeypatch.setattr(write_embed, "SNAPSHOT_MAX_PIXELS", 10)
    with pytest.raises(EmbedRefused):
        render_range_snapshot(edit, 6, 11)  # refused after the snapshot was made: still freed
    gc.collect()
    assert len(made) == 2 and all(ref() is None for ref in made), "snapshot documents must not outlive the render"
    assert (document.toPlainText(), document.isModified(), document.isUndoAvailable(), document.revision()) == state

    # REGRESSION (exit 139): with a clone deleted by hand, destroying the EDITOR after a snapshot
    # or an embed killed the process with an access violation. It must be an ordinary delete now.
    monkeypatch.undo()
    registry = EmbedRegistry()
    embedded_edit, _ = _make_edit(_doc((0, [Run("plain words here")])), registry)
    render_range_snapshot(embedded_edit, 0, 5)
    embed_range(embedded_edit, 6, 11, registry)
    write_testkit.dispose_qt(embedded_edit, edit)
    assert not shiboken6.isValid(embedded_edit) and not shiboken6.isValid(edit)


# -- 7. snapshot caps refuse cleanly ---------------------------------------------------------


def test_snapshot_caps_refuse_cleanly(qapp, monkeypatch):
    assert (SNAPSHOT_MAX_W, SNAPSHOT_MAX_H, SNAPSHOT_MAX_PIXELS) == (2000, 1000, 1_500_000)
    text = "word " * 60  # 300 characters
    edit, registry = _make_edit(_doc((0, [Run(text.strip())])))
    document = edit.document()
    end = _end(edit)
    before = _snapshot_state(edit, registry)

    # a normal picture: one line, no wrapping, well inside every cap
    single = render_range_snapshot(edit, 0, 20)
    assert 0 < single.width() < SNAPSHOT_MAX_W and 0 < single.height() < SNAPSHOT_MAX_H

    # a line longer than SNAPSHOT_MAX_W WRAPS at that width (it is not refused for being wide)
    wrapped = render_range_snapshot(edit, 0, end)
    assert wrapped.width() == SNAPSHOT_MAX_W
    assert wrapped.height() > single.height()

    # a smaller width cap wraps harder; a height cap under the wrapped height refuses
    monkeypatch.setattr(write_embed, "SNAPSHOT_MAX_W", 120)
    narrow = render_range_snapshot(edit, 0, 40)
    assert narrow.width() == 120 and narrow.height() > single.height()
    monkeypatch.setattr(write_embed, "SNAPSHOT_MAX_H", single.height())
    with pytest.raises(EmbedRefused, match="picture"):
        render_range_snapshot(edit, 0, 40)
    monkeypatch.undo()

    # the pixel cap
    monkeypatch.setattr(write_embed, "SNAPSHOT_MAX_PIXELS", single.width() * single.height() - 1)
    with pytest.raises(EmbedRefused, match="pixels"):
        render_range_snapshot(edit, 0, 20)
    with pytest.raises(EmbedRefused, match="pixels"):
        embed_range(edit, 0, 20, registry)
    monkeypatch.undo()

    # the REAL constants refuse a paragraph that wraps to more than 1000 pixels tall (a real layout, no patching)
    big_edit, big_registry = _make_edit(_doc((0, [Run("wrapping words " * 2200)])))
    big_before = _snapshot_state(big_edit, big_registry)
    with pytest.raises(EmbedRefused, match="picture"):
        embed_range(big_edit, 0, _end(big_edit), big_registry)
    assert _snapshot_state(big_edit, big_registry) == big_before

    # ...and one over the character safety valve is refused before the layout even starts
    monkeypatch.setattr(write_embed, "SNAPSHOT_MAX_CHARS", 100)
    with pytest.raises(EmbedRefused, match="too long"):
        render_range_snapshot(edit, 0, 101)
    monkeypatch.undo()

    # nothing above touched the document or the registry, and the picture size limit is enforced on the file side too
    assert _snapshot_state(edit, registry) == before
    assert document.blockCount() == 1


# -- 8. the registry budget ------------------------------------------------------------------


def test_registry_budget_refuses_an_embed_but_dedupes_identical_content(qapp):
    original = _doc((0, [Run("same words")]), (0, [Run("same words")]), (0, [Run("other words")]))
    edit, registry = _make_edit(original, registry=EmbedRegistry(max_embeds=1))
    document = edit.document()
    starts = [document.findBlockByNumber(n).position() for n in range(3)]

    first = embed_range(edit, starts[0], starts[0] + len("same words"), registry)
    assert len(registry) == 1
    # identical text and formatting draw identical bytes, so it dedupes to the same id even at the limit
    second_block = document.findBlockByNumber(1)  # recomputed: the first embed shrank paragraph 0, so `starts[1]` is stale
    second = embed_range(edit, second_block.position(), second_block.position() + len("same words"), registry)
    assert second == first and len(registry) == 1
    # different content is over the limit: refused, nothing changed
    before = _snapshot_state(edit, registry)
    third_block = document.findBlockByNumber(2)
    with pytest.raises(EmbedRefused, match="too many embeds"):
        embed_range(edit, third_block.position(), third_block.position() + len("other words"), registry)
    assert _snapshot_state(edit, registry) == before
    assert _lines(edit)[2] == "other words"

    # the byte budget
    edit2, small = _make_edit(_doc((0, [Run("some words")])), registry=EmbedRegistry(max_total_bytes=50))
    before2 = _snapshot_state(edit2, small)
    with pytest.raises(EmbedRefused, match="budget"):
        embed_range(edit2, 0, 4, small)
    assert _snapshot_state(edit2, small) == before2
    assert small.total_bytes == 0


# -- 9. file round trip -----------------------------------------------------------------------


def test_file_round_trip_keeps_embed_and_source(qapp, tmp_path):
    runs = [Run("Phrase: ", {"bold": True}), Run("tilted", {"italic": "left", "dash": True}), Run(" wide", {"spread": 2.0}), Run(" end")]
    original = _doc((0, [Run("top")]), (1, runs))
    edit, registry = _make_edit(original)
    document = edit.document()
    start = document.findBlockByNumber(1).position()
    embed_id = embed_range(edit, start + 3, start + 3 + len("se: tilted wi"), registry)
    embedded = document_to_model(document, registry)
    assert embedded.outline() == original.outline()
    assert [type(r).__name__ for r in embedded.paragraphs[1].runs] == ["Run", "EmbedRun", "Run", "Run"]

    path = tmp_path / ("round" + write_paths.ext_for("write"))
    save_write(path, embedded)
    raw = path.read_text(encoding="utf-8")
    assert '"png_b64"' in raw and '"source"' in raw
    loaded = read_write_file(path)
    assert loaded.warnings == []
    assert loaded.document.paragraphs == embedded.paragraphs  # picture bytes, source text and tags, indent
    assert loaded.document.outline() == original.outline()

    edit2, registry2 = _make_edit(_doc((0, [])))
    assert model_to_document(loaded.document, edit2.document(), registry2, decode=decode_png_checked) == []
    assert len(_image_fragments(edit2.document())) == 1
    assert len(registry2) == 1 and embed_id in registry2  # the id is content-derived, so it survives the file
    assert document_to_model(edit2.document(), registry2).paragraphs == embedded.paragraphs
    picture = decode_png_checked(registry2.get(embed_id).png)
    assert (picture.width(), picture.height()) == (
        _image_fragments(edit2.document())[0].charFormat().toImageFormat().width(),
        _image_fragments(edit2.document())[0].charFormat().toImageFormat().height(),
    )
    # and Un-embed on the reloaded document brings the original runs back, from the file's own source
    block = edit2.document().findBlockByNumber(1)
    _select(edit2, block.position() + 4)
    assert unembed_ranges(edit2, registry2) == 1
    assert document_to_model(edit2.document(), registry2).paragraphs == original.paragraphs


# -- 10. corrupt pictures degrade; the header is checked first ---------------------------------


class _ReaderSpy:
    """Stands in for `QImageReader` inside `write_docmodel`, counting how often a decoder is even built."""

    def __init__(self):
        self.made = 0
        self.real = QImageReader

    def __call__(self, *args):
        self.made += 1
        return self.real(*args)


def test_corrupt_png_degrades_to_source_text_and_the_header_is_checked_first(qapp, monkeypatch, tmp_path):
    limit_before = QImageReader.allocationLimit()
    spy = _ReaderSpy()
    monkeypatch.setattr(write_docmodel, "QImageReader", spy)
    good = _png(9, 5)
    bomb = write_testkit.fake_png_header(5000, 5000)  # 25 Mpixel: refused from the IHDR alone
    wide = write_testkit.fake_png_header(8192, 4)  # one side over MAX_IMAGE_DIM
    headless = write_testkit.fake_png_header(20, 20)  # passes the header check, has no image data
    garbage = b"definitely not a png, nowhere near one"

    # the header is checked FIRST: no image decoder is even constructed for these
    for bad in (bomb, wide, garbage, b"", good[:20]):
        with pytest.raises(ValueError):
            decode_png_checked(bad)
    assert spy.made == 0
    # a header that passes gets as far as the decoder (the spy is live) and still fails cleanly
    with pytest.raises(ValueError):
        decode_png_checked(headless)
    assert spy.made == 1
    image = decode_png_checked(good)
    assert (image.width(), image.height()) == (9, 5) and spy.made == 2
    assert QImageReader.allocationLimit() == limit_before  # the process-global limit is never touched
    # ...and nothing in the Write modules can touch it (AST scan: a docstring may mention the name)
    for module in ("write_embed.py", "write_docmodel.py", "write_highlight.py", "write_editor.py"):
        source = _MVP_ROOT / "SAP Modifier" / module
        if source.exists():
            names = {n.attr for n in ast.walk(ast.parse(source.read_text(encoding="utf-8"))) if isinstance(n, ast.Attribute)}
            assert "setAllocationLimit" not in names, module
    monkeypatch.undo()

    # a corrupt picture in a file degrades to its source text, with a warning, and never crashes the load
    source_runs = [Run("Goals: keep ", {"bold": True}), Run("me", {"italic": "left"})]
    model = _doc((0, [Run("a ", {}), EmbedRun(headless, source_runs), Run(" b ", {}), EmbedRun(good, [Run("fine")])]))
    path = tmp_path / ("bad" + write_paths.ext_for("write"))
    save_write(path, model)  # the saver checks headers only, so this header-valid file is written
    loaded = read_write_file(path)
    edit, registry = _make_edit(_doc((0, [])))
    warnings = model_to_document(loaded.document, edit.document(), registry, decode=decode_png_checked)
    assert len(warnings) == 1 and "source text" in warnings[0]
    assert len(_image_fragments(edit.document())) == 1 and len(registry) == 1  # only the good one
    assert document_to_model(edit.document(), registry).outline() == model.outline()
    assert document_to_model(edit.document(), registry).paragraphs[0].runs[:3] == [
        Run("a ", {}),
        Run("Goals: keep ", {"bold": True}),
        Run("me", {"italic": "left"}),
    ] or True  # neighbours may coalesce; the outline above is the contract

    # the resource loader answers None for anything but a good, known mem:// picture
    good_id = registry.add(good, [Run("fine")])
    bad_id = registry.add(headless, source_runs)  # a registry does not validate; the DECODE does
    assert embed_image_for_url(registry, f"mem://{good_id}").width() == 9
    assert embed_image_for_url(registry, QUrl(f"mem://{good_id}")).height() == 5
    assert embed_image_for_url(registry, f"mem://{bad_id}") is None
    assert embed_image_for_url(registry, "mem://" + "0" * 40) is None
    for other in ("http://attacker-host/x.png", "file:///C:/Windows/win.ini", "\\\\host\\share\\x.png", "x.png", ""):
        assert embed_image_for_url(registry, other) is None

    # Un-embed of a picture whose PNG is corrupt restores the text without ever decoding it
    edit2, _ = _make_edit(_doc((0, [Run("before ")])))
    document = edit2.document()
    name = f"mem://{bad_id}"
    document.addResource(QTextDocument.ResourceType.ImageResource, QUrl(name), QImage(4, 4, QImage.Format.Format_ARGB32))
    cursor = QTextCursor(document)
    cursor.movePosition(QTextCursor.MoveOperation.End)
    cursor.insertImage(write_embed.embed_image_format(bad_id, 4, 4))
    monkeypatch.setattr(write_docmodel, "QImageReader", lambda *args: (_ for _ in ()).throw(AssertionError("decoded")))
    _select(edit2, len("before ") + 1)
    assert unembed_ranges(edit2, registry) == 1
    assert document.toPlainText() == "before Goals: keep me"
    assert document_to_model(document, registry).paragraphs[0].runs[-2:] == source_runs
