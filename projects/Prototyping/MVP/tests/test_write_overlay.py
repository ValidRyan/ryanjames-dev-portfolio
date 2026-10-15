"""Tests for write_overlay.py - Dash diagonal and left-lean painting (hazards A, 2, 3).

The FIRST test is the never-shown-editor canary: a violated guard in the overlay
is a native crash (exit 139) that kills the whole pytest process, so it runs
before anything that could hide it. Pixel tests assert RATIOS and BAND
CENTROIDS measured inside one image (never absolute pixel positions), use an
explicit palette, and skip when a platform draws no ink at all. Note: the
offscreen platform here has no fonts installed, so text renders as "tofu" boxes
- the tests are shape-independent by design (they separate the base text from the
overlay by COLOUR, using a distinct overlay palette, not by glyph shape).

Every test runs under the `write_qt` fixture and registers the widgets it builds
with `write_testkit.track_qt` (see write_testkit's Qt lifetime rules): a stray
widget from another test file once crashed `QTest.qWait` here. Source stays
ASCII; printed strings go through `ascii()`.
"""

import sys
from pathlib import Path

_MVP_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_MVP_ROOT / "SAP Modifier"))

import pytest
from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QImage, QPainter, QPalette, QTextCursor, QTextDocument
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QTextEdit

import write_overlay
import write_testkit
from write_docmodel import char_format_from_tags, model_to_document
from write_model import EmbedRegistry, Paragraph, Run, WriteDocument
from write_overlay import LEFT_LEAN_SHEAR, RunSegment, collect_segments, paint_overlays, paint_segments
from write_testkit import write_qt  # noqa: F401  (fixture: see write_testkit's Qt lifetime rules)

pytestmark = pytest.mark.usefixtures("write_qt")

WHITE = QColor(255, 255, 255)
BLACK = QColor(0, 0, 0)
RED = QColor(255, 0, 0)
BLUE = QColor(0, 0, 200)


def _palette(base=WHITE, text=BLACK, highlight=BLUE, highlighted_text=WHITE) -> QPalette:
    """An explicit palette; `setColor(role, colour)` sets every colour group at once."""
    pal = QPalette()
    pal.setColor(QPalette.ColorRole.Base, base)
    pal.setColor(QPalette.ColorRole.Window, base)
    pal.setColor(QPalette.ColorRole.Text, text)
    pal.setColor(QPalette.ColorRole.WindowText, text)
    pal.setColor(QPalette.ColorRole.Highlight, highlight)
    pal.setColor(QPalette.ColorRole.HighlightedText, highlighted_text)
    return pal


def _model(*paragraphs) -> WriteDocument:
    return WriteDocument([Paragraph(0, list(runs)) for runs in paragraphs])


def _model_indented(indent, runs) -> WriteDocument:
    return WriteDocument([Paragraph(indent, list(runs))])


class _OverlayEdit(QTextEdit):
    """A plain `QTextEdit` that paints the overlays the way `WriteEditor` will.

    `overlay_palette` may differ from the widget's own palette: the base text
    is drawn by Qt in the widget palette (say RED), the overlay redraw in the
    overlay palette (BLACK), so any RED pixel left in the image is a ghost of
    the original glyphs and any BLACK pixel is overlay ink.
    """

    def __init__(self, overlay_palette=None, draw_overlay=True):
        super().__init__()
        self.overlay_palette = overlay_palette
        self.draw_overlay = draw_overlay
        self.covers = []

    def paintEvent(self, event):
        super().paintEvent(event)
        if not self.draw_overlay:
            return
        painter = QPainter(self.viewport())
        try:
            offset = QPointF(-self.horizontalScrollBar().value(), -self.verticalScrollBar().value())
            cursor = self.textCursor()
            selection = (cursor.selectionStart(), cursor.selectionEnd()) if cursor.hasSelection() else None
            self.covers = paint_overlays(
                painter,
                self.document(),
                offset,
                QRectF(self.viewport().rect()),
                self.overlay_palette or self.palette(),
                selection,
            )
        finally:
            painter.end()


def _make_edit(model, *, size=(480, 160), point_size=30, bold=False, widget_palette=None, overlay_palette=None, draw_overlay=True):
    edit = write_testkit.track_qt(_OverlayEdit(overlay_palette, draw_overlay))
    edit.setPalette(widget_palette or _palette())
    edit.setFrameShape(QTextEdit.Shape.NoFrame)
    edit.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    edit.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    font = QFont()
    font.setPointSize(point_size)
    font.setBold(bold)
    edit.setFont(font)
    model_to_document(model, edit.document(), EmbedRegistry())
    edit.resize(*size)
    return edit


def _grab(edit) -> QImage:
    """Render the editor (forces a real layout and a real paintEvent)."""
    return edit.grab().toImage().convertToFormat(QImage.Format.Format_ARGB32)


def _pixels(image: QImage):
    """Yield `(x, y, r, g, b)` for every pixel."""
    for y in range(image.height()):
        for x in range(image.width()):
            c = image.pixelColor(x, y)
            yield x, y, c.red(), c.green(), c.blue()


def _ink_points(image: QImage, predicate):
    return [(x, y) for x, y, r, g, b in _pixels(image) if predicate(r, g, b)]


def _dark(r, g, b) -> bool:  # overlay ink on a white base
    return r < 90 and g < 90 and b < 90


def _reddish(r, g, b) -> bool:  # ghost / base ink when the widget palette Text is RED
    return r > 170 and g < 90 and b < 90


def _band_centroid_x(points, y_lo, y_hi):
    xs = [x for x, y in points if y_lo <= y <= y_hi]
    return (sum(xs) / len(xs)) if xs else None


def _upper_lower_shift(points) -> float:
    """(upper-band centroid x - lower-band centroid x) / (vertical distance between the bands).

    A slope-like ratio, independent of glyph shape and absolute position:
    about 0 for upright ink, negative when the top leans LEFT, positive for RIGHT.
    """
    ys = [y for _, y in points]
    top, bottom = min(ys), max(ys)
    span = bottom - top
    if span < 8:
        return None
    third = span / 3.0
    upper = _band_centroid_x(points, top, top + third)
    lower = _band_centroid_x(points, bottom - third, bottom)
    if upper is None or lower is None:
        return None
    upper_y = sum(y for _, y in points if top <= y <= top + third) / len([1 for _, y in points if top <= y <= top + third])
    lower_y = sum(y for _, y in points if bottom - third <= y <= bottom) / len([1 for _, y in points if bottom - third <= y <= bottom])
    return (upper - lower) / (lower_y - upper_y)


LEAN = {"italic": "left"}
DASH = {"dash": True}


# -- 1. the canary (keep first) -------------------------------------------------


def test_never_shown_editor_collects_nothing_and_does_not_crash(qapp):
    model = _model(
        [Run("plain ", {}), Run("tilted words", LEAN), Run(" struck", DASH), Run(" both", {"italic": "left", "dash": True})],
        [],
        [Run("second paragraph", LEAN)],
    )
    edit = write_testkit.track_qt(QTextEdit())  # never shown, never sized
    model_to_document(model, edit.document(), EmbedRegistry())
    doc = edit.document()
    clip = QRectF(0, 0, 400, 300)

    # precondition (V2): nothing is laid out, so lineAt() would crash if it were ever reached
    layout = doc.documentLayout()
    assert layout is not None
    block = doc.begin()
    while block.isValid():
        assert block.layout().lineCount() == 0
        assert layout.blockBoundingRect(block).isNull()
        block = block.next()

    assert collect_segments(doc, QPointF(0, 0), clip) == []
    assert collect_segments(doc, QPointF(-3.0, -40.0), QRectF(0, 0, 10, 10)) == []

    # a hidden resize() does not lay the document out either (the resize event is deferred)
    edit.resize(400, 300)
    assert doc.begin().layout().lineCount() == 0
    assert collect_segments(doc, QPointF(0, 0), clip) == []

    # painting the overlay into an off-screen image is a no-op, not a crash
    image = QImage(400, 300, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(WHITE)
    reference = image.copy()
    painter = QPainter(image)
    try:
        covers = paint_overlays(painter, doc, QPointF(0, 0), clip, _palette(), (0, 6))
    finally:
        painter.end()
    assert covers == []
    assert image == reference
    assert paint_segments(QPainter(), [], palette=_palette(), selection=None) == []

    # A bare document (no widget) has a usable page size, so asking for a block rect
    # lays the block out first: the block-rect gate is what makes lineAt() safe there,
    # because it runs BEFORE any lineAt() and forces the layout that lineAt() needs.
    bare = QTextDocument()
    cursor = QTextCursor(bare)
    cursor.insertText("bare tilted", char_format_from_tags(LEAN))
    assert bare.begin().layout().lineCount() == 0
    found = collect_segments(bare, QPointF(0, 0), clip)
    assert [(s.text, s.lean, s.dash) for s in found] == [("bare tilted", True, False)]
    assert bare.begin().layout().lineCount() > 0


# -- 2. geometry after show / resize / wrap ---------------------------------------


def _shown_edit(model, size=(300, 200), point_size=None):
    edit = write_testkit.track_qt(QTextEdit())
    if point_size:
        font = QFont()
        font.setPointSize(point_size)
        edit.setFont(font)
    model_to_document(model, edit.document(), EmbedRegistry())
    edit.resize(*size)
    edit.show()
    QTest.qWait(30)
    return edit


def _viewport_clip(edit) -> QRectF:
    return QRectF(edit.viewport().rect())


def test_segments_after_show_resize_and_wrap(qapp):
    long_run = "tilted words wrapping around the narrow editor again and again"
    model = _model(
        [Run("plain ", {}), Run("lean", LEAN), Run(" struck", DASH), Run(" both", {"italic": "left", "dash": True})],
        [Run(long_run, LEAN)],
        [Run("upright only", {"bold": True})],
    )
    edit = _shown_edit(model, size=(300, 260))
    doc = edit.document()
    clip = _viewport_clip(edit)

    segments = collect_segments(doc, QPointF(0, 0), clip)
    assert segments, "a shown editor must yield segments"
    for seg in segments:
        assert isinstance(seg, RunSegment)
        assert seg.text == seg.block.text()[seg.lo : seg.hi] and seg.lo < seg.hi
        assert seg.x1 >= seg.x0 and seg.line_h > 0
        assert seg.line_top < seg.baseline <= seg.line_top + seg.line_h
        assert seg.dash or seg.lean
    first_line = [s for s in segments if s.block.blockNumber() == 0]
    assert [(s.text, s.lean, s.dash) for s in first_line] == [
        ("lean", True, False),
        (" struck", False, True),
        (" both", True, True),
    ]
    assert not any(s.block.blockNumber() == 2 for s in segments)  # upright paragraph: nothing to paint

    # a wrapped run yields one segment per visual line, tiling the run without gaps
    wrapped = [s for s in segments if s.block.blockNumber() == 1]
    block = doc.findBlockByNumber(1)
    assert block.layout().lineCount() >= 3
    assert len(wrapped) == block.layout().lineCount()
    assert "".join(s.text for s in wrapped) == long_run
    assert all(a.hi == b.lo for a, b in zip(wrapped, wrapped[1:]))
    assert [s.line_top for s in wrapped] == sorted({s.line_top for s in wrapped})  # strictly increasing lines

    # the offset is a pure translation of painter space
    shifted = collect_segments(doc, QPointF(-5.0, -7.0), QRectF(clip).translated(-5.0, -7.0))
    assert len(shifted) == len(segments)
    for a, b in zip(segments, shifted):
        assert (b.x0, b.x1, b.line_top, b.baseline) == pytest.approx((a.x0 - 5.0, a.x1 - 5.0, a.line_top - 7.0, a.baseline - 7.0))

    # the clip limits which visual lines are collected
    top_line = wrapped[0]
    narrow = collect_segments(doc, QPointF(0, 0), QRectF(0, top_line.line_top, 300, 1))
    assert {s.line_top for s in narrow if s.block.blockNumber() == 1} == {top_line.line_top}

    # widening the editor re-wraps: the same run is now a single segment
    edit.resize(2600, 260)
    QTest.qWait(30)
    wide = [s for s in collect_segments(doc, QPointF(0, 0), _viewport_clip(edit)) if s.block.blockNumber() == 1]
    assert len(wide) == 1 and wide[0].text == long_run

    # hiding does not invalidate what is already laid out, and never crashes
    edit.hide()
    assert isinstance(collect_segments(doc, QPointF(0, 0), clip), list)


# -- 3. only the visible blocks are visited (hazard P2) ----------------------------


class _CountingLayout:
    def __init__(self, layout):
        self._layout = layout
        self.rect_calls = 0

    def blockBoundingRect(self, block):
        self.rect_calls += 1
        return self._layout.blockBoundingRect(block)

    def __getattr__(self, name):
        return getattr(self._layout, name)


class _CountingDoc:
    """Delegates to a real `QTextDocument`, counting the calls a linear scan would multiply."""

    def __init__(self, doc):
        self._doc = doc
        self.layout = _CountingLayout(doc.documentLayout())
        self.find_calls = 0

    def documentLayout(self):
        return self.layout

    def findBlockByNumber(self, number):
        self.find_calls += 1
        return self._doc.findBlockByNumber(number)

    def __getattr__(self, name):
        return getattr(self._doc, name)


def test_scan_visits_only_visible_blocks(qapp):
    count = 3000
    model = WriteDocument([Paragraph(0, [Run(f"row {n} ", {}), Run("tilted", LEAN)]) for n in range(count)])
    edit = _shown_edit(model, size=(320, 140))
    doc = edit.document()
    assert doc.blockCount() == count
    bar = edit.verticalScrollBar()
    assert bar.maximum() > 0

    def collect_counted():
        proxy = _CountingDoc(doc)
        found = collect_segments(proxy, QPointF(-edit.horizontalScrollBar().value(), -bar.value()), _viewport_clip(edit))
        return found, proxy

    # at the top: a handful of segments, a handful of rect queries (not thousands)
    found, proxy = collect_counted()
    assert 3 <= len(found) < 40
    assert proxy.layout.rect_calls < 80 and proxy.find_calls < 40

    # scrolled to the very end: still cheap, and the segments are the LAST blocks (no scan from block 0)
    bar.setValue(bar.maximum())
    QTest.qWait(30)
    found, proxy = collect_counted()
    assert found
    numbers = [s.block.blockNumber() for s in found]
    assert min(numbers) > count - 60 and max(numbers) == count - 1
    assert proxy.layout.rect_calls < 80 and proxy.find_calls < 40
    for seg in found:  # the offset put every segment inside the viewport
        assert seg.line_top < _viewport_clip(edit).bottom() and seg.line_top + seg.line_h > 0

    # a middle scroll position finds the middle blocks
    bar.setValue(bar.maximum() // 2)
    QTest.qWait(30)
    found, proxy = collect_counted()
    numbers = [s.block.blockNumber() for s in found]
    assert numbers and count * 0.3 < min(numbers) < max(numbers) < count * 0.7
    assert proxy.layout.rect_calls < 80


# -- pixel tests ------------------------------------------------------------------


def _red_base_edit(model, *, overlay=True, **kwargs):
    """Base text drawn RED by Qt; the overlay draws BLACK: colour tells the two apart."""
    return _make_edit(
        model,
        widget_palette=_palette(text=RED),
        overlay_palette=_palette(text=BLACK),
        draw_overlay=overlay,
        **kwargs,
    )


def test_dash_draws_a_rising_diagonal(qapp):
    model = _model([Run("IIII", DASH)], [Run("IIII", {})])
    base = _grab(_red_base_edit(model, overlay=False))
    if not _ink_points(base, _reddish):
        pytest.skip("platform drew no text ink")
    assert not _ink_points(base, _dark), "control: without the overlay nothing dark exists"

    edit = _red_base_edit(model)
    image = _grab(edit)
    points = _ink_points(image, _dark)
    assert points, "the overlay must draw the diagonal in the palette Text colour"
    seg = collect_segments(edit.document(), QPointF(0, 0), _viewport_clip(edit))
    assert len(seg) == 1 and seg[0].dash and not seg[0].lean

    xs = [x for x, _ in points]
    ys = [y for _, y in points]
    # (1) only the dashed paragraph has ink: everything sits in that line's band
    assert min(ys) >= seg[0].line_top - 1 and max(ys) <= seg[0].line_top + seg[0].line_h + 1
    # (2) the extent matches the run width (ratio, not pixels)
    assert (max(xs) - min(xs)) / (seg[0].x1 - seg[0].x0) == pytest.approx(1.0, abs=0.06)
    # (3) it RISES to the right: the left third sits lower (larger y) than the right third
    span = max(xs) - min(xs)
    left = [(x, y) for x, y in points if x <= min(xs) + span / 3]
    right = [(x, y) for x, y in points if x >= max(xs) - span / 3]
    left_y = sum(y for _, y in left) / len(left)
    right_y = sum(y for _, y in right) / len(right)
    assert left_y > right_y
    # (4) its slope matches the segment's own diagonal (corner to corner, one pixel inset)
    left_x = sum(x for x, _ in left) / len(left)
    right_x = sum(x for x, _ in right) / len(right)
    slope = (left_y - right_y) / (right_x - left_x)
    expected = (seg[0].line_h - 2.0) / (seg[0].x1 - seg[0].x0)
    assert slope / expected == pytest.approx(1.0, abs=0.25)


def test_left_lean_shears_ink_leftward_versus_upright_control(qapp):
    """Fact V4: `shear(+0.30, 0)` must lean LEFT - decided by ink centroids, not by trust."""
    assert LEFT_LEAN_SHEAR == pytest.approx(0.30)
    # a plain space prefix keeps the tilted top-left corner inside the viewport (nothing is clipped)
    lean_model = _model([Run("    ", {}), Run("MMMM", LEAN)])
    upright_model = _model([Run("    MMMM", {})])
    control = _make_edit(upright_model, widget_palette=_palette(text=BLACK), draw_overlay=False, point_size=36)
    control_points = _ink_points(_grab(control), _dark)
    if not control_points:
        pytest.skip("platform drew no text ink")

    edit = _red_base_edit(lean_model, point_size=36)
    lean_points = _ink_points(_grab(edit), _dark)
    assert lean_points, "the redraw must produce dark ink"

    upright_shift = _upper_lower_shift(control_points)
    lean_shift = _upper_lower_shift(lean_points)
    assert upright_shift is not None and lean_shift is not None
    assert abs(upright_shift) < 0.06, "control: upright ink is vertical"
    # the upper band is displaced LEFT of the lower band, by about the shear constant
    assert lean_shift < upright_shift - 0.15
    assert lean_shift / -LEFT_LEAN_SHEAR == pytest.approx(1.0, abs=0.4)

    # native right italic never leans further left than the upright control does
    right_edit = _make_edit(_model([Run("    MMMM", {"italic": "right"})]), widget_palette=_palette(text=BLACK), draw_overlay=False, point_size=36)
    right_shift = _upper_lower_shift(_ink_points(_grab(right_edit), _dark))
    if right_shift is not None:
        assert right_shift >= upright_shift - 0.06 and right_shift > lean_shift


def _in_run_box(points, seg, pad=1):
    """The points inside a segment's advance box (plus `pad` px sideways): the run's own ink only.

    The offscreen platform here has no fonts, so even the plain prefix spaces are
    drawn as boxes. A ghost count over the whole image would blame the overlay
    for glyphs it never owned; the cover only promises to hide the run it tilts.
    """
    return [(x, y) for x, y in points if seg.x0 - pad <= x < seg.x1 + pad and seg.line_top <= y < seg.line_top + seg.line_h]


def test_no_upright_ghost_survives_the_cover(qapp):
    model = _model([Run("    ", {}), Run("I", LEAN)])
    # a wide viewport: with no fonts every glyph is a big box, and a run clipped by the viewport edge would be measured, not the overlay
    kwargs = {"bold": True, "point_size": 48, "size": (720, 130)}
    base = _grab(_red_base_edit(model, overlay=False, **kwargs))
    if not _ink_points(base, _reddish):
        pytest.skip("platform drew no text ink")

    edit = _red_base_edit(model, **kwargs)
    image = _grab(edit)
    segments = collect_segments(edit.document(), QPointF(0, 0), _viewport_clip(edit))
    assert len(segments) == 1 and segments[0].lean
    seg = segments[0]
    base_red = _in_run_box(_ink_points(base, _reddish), seg)
    assert base_red, "control: without the overlay Qt draws upright ink inside the run"
    dark = _ink_points(image, _dark)
    ghost = _in_run_box(_ink_points(image, _reddish), seg)
    assert dark, "the sheared redraw must be there"
    # every upright base pixel of the run is covered: what survives is a sliver of what Qt drew
    assert len(ghost) / len(base_red) < 0.02
    assert len(ghost) / len(dark) < 0.05
    # and the redraw leans, i.e. it is not just the upright glyph repainted in another colour
    shift = _upper_lower_shift(dark)
    assert shift is not None and shift < -0.15


def test_lean_honours_spread_bold_and_strike(qapp):
    def dark_of(tags):
        merged = dict(tags)
        merged["italic"] = "left"
        edit = _red_base_edit(_model([Run("    ", {}), Run("MMMM", merged)]), point_size=28, size=(760, 110))
        return edit, _ink_points(_grab(edit), _dark)

    def extent(points):
        xs = [x for x, _ in points]
        return max(xs) - min(xs)

    control = _make_edit(_model([Run("    MMMM", {})]), widget_palette=_palette(text=BLACK), draw_overlay=False, point_size=28, size=(760, 110))
    if not _ink_points(_grab(control), _dark):
        pytest.skip("platform drew no text ink")

    _, plain = dark_of({})
    wide_edit, wide = dark_of({"spread": 14.0})
    assert plain and wide
    # spread: the redrawn run is markedly wider than the unspread one (ratio)
    assert 1.2 < extent(wide) / extent(plain) < 1.9

    # ...and it is as wide as Qt's own upright layout of the same spread run (ratio ~ 1; the tilt adds a little).
    # The control's ink is cut at the run's start: with no fonts the four plain prefix spaces are drawn
    # as boxes too, and they belong to neither the redraw nor the run under test.
    run_x0 = collect_segments(wide_edit.document(), QPointF(0, 0), _viewport_clip(wide_edit))[0].x0
    upright_wide = _make_edit(_model([Run("    ", {}), Run("MMMM", {"spread": 14.0})]), widget_palette=_palette(text=BLACK), draw_overlay=False, point_size=28, size=(760, 110))
    upright_run = [(x, y) for x, y in _ink_points(_grab(upright_wide), _dark) if x >= run_x0]
    assert upright_run, "control: Qt draws ink for the upright spread run"
    assert extent(wide) / extent(upright_run) == pytest.approx(1.0, abs=0.2)

    # bold: the segment carries the bold font, and the redraw keeps the layout's extent
    edit, bold_points = dark_of({"bold": True})
    seg = collect_segments(edit.document(), QPointF(0, 0), _viewport_clip(edit))[0]
    assert seg.font.bold() and seg.font.weight() >= 600
    assert extent(bold_points) / extent(plain) == pytest.approx(1.0, abs=0.25)

    # strike-out travels in the font: the redraw with a line has more ink than without
    edit, struck = dark_of({"line": True})
    assert collect_segments(edit.document(), QPointF(0, 0), _viewport_clip(edit))[0].font.strikeOut()
    assert len(struck) > len(plain) * 1.03

    # the segment font also carries the absolute spacing (this is what widens the redraw)
    edit, _ = dark_of({"spread": 14.0})
    seg = collect_segments(edit.document(), QPointF(0, 0), _viewport_clip(edit))[0]
    assert seg.font.letterSpacingType() == QFont.SpacingType.AbsoluteSpacing
    assert seg.font.letterSpacing() == pytest.approx(14.0)


def test_overlay_geometry_follows_the_block_indent(qapp):
    """Qt bakes a block's indent into `QTextLine.x()`, so the layout origin is the block rect's corner.

    An origin that subtracts the layout's own bounding corner cancels the indent
    and puts every cover, redraw and diagonal `indent * indentWidth` px left of
    the real text: the upright text stays visible, uncovered, beside a misplaced
    copy. The Indent tool makes indented Write paragraphs the normal case.
    """

    def scene(tags, indent):
        model = _model_indented(indent, [Run("MMMM", tags)])
        kwargs = {"point_size": 20, "size": (320, 48)}  # small: the pixel scans are pure Python
        base = _grab(_red_base_edit(model, overlay=False, **kwargs))
        edit = _red_base_edit(model, **kwargs)
        image = _grab(edit)
        segments = collect_segments(edit.document(), QPointF(0, 0), _viewport_clip(edit))
        assert len(segments) == 1
        return edit, segments[0], base, image

    for tags in (LEAN, DASH):
        zero_x0 = None
        for indent in (0, 1, 3):
            edit, seg, base, image = scene(tags, indent)
            red = _ink_points(base, _reddish)
            if not red:
                pytest.skip("platform drew no text ink")
            step = edit.document().indentWidth()
            assert step > 0
            if indent == 0:
                zero_x0 = seg.x0
            # geometry only (no fonts needed): the segment moves right by exactly one indent step per level
            assert seg.x0 == pytest.approx(zero_x0 + indent * step)
            # ...and the segment box is where Qt really drew the run
            assert min(x for x, _ in red) >= seg.x0 - 1 and max(x for x, _ in red) <= seg.x1 + 1
            dark = _ink_points(image, _dark)
            assert dark, "the overlay must draw something"
            if tags is LEAN:
                inside = _in_run_box(red, seg)
                assert inside and len(_in_run_box(_ink_points(image, _reddish), seg)) / len(inside) < 0.02
                centroid = sum(x for x, _ in dark) / len(dark)
                assert seg.x0 <= centroid <= seg.x1  # the tilted copy sits over the text, not indent px away from it
            else:
                xs = [x for x, _ in dark]
                assert min(xs) >= seg.x0 - 1 and max(xs) <= seg.x1 + 1
                assert (max(xs) - min(xs)) / (seg.x1 - seg.x0) == pytest.approx(1.0, abs=0.06)


def test_selection_over_left_lean_leaves_no_ghost(qapp):
    # Qt draws selected text in the WIDGET palette's HighlightedText (RED here); the overlay
    # redraws it in ITS palette's HighlightedText (WHITE) on Highlight (BLUE): colour separates them.
    widget = _palette(text=RED, highlight=BLUE, highlighted_text=RED)
    overlay = _palette(text=BLACK, highlight=BLUE, highlighted_text=WHITE)

    def whiteish(r, g, b):
        return r > 200 and g > 200 and b > 200

    model = _model([Run("lean ", {}), Run("MMMM", LEAN), Run(" tail", {})])

    def make(select, overlay_on=True):
        edit = _make_edit(model, widget_palette=widget, overlay_palette=overlay, draw_overlay=overlay_on, point_size=32, size=(520, 100))
        cursor = edit.textCursor()
        cursor.setPosition(select[0])
        cursor.setPosition(select[1], QTextCursor.MoveMode.KeepAnchor)
        edit.setTextCursor(cursor)
        return edit

    run_lo, run_hi = len("lean "), len("lean MMMM")
    everything = (0, len("lean MMMM tail"))

    # control: with the overlay off the selected upright glyphs are plainly visible in RED on BLUE
    base = _grab(make(everything, overlay_on=False))
    base_red = _ink_points(base, _reddish)
    if not base_red:
        pytest.skip("platform drew no selected text ink")

    # whole line selected: the tilted run is redrawn white-on-blue, no red survives inside the run's cover
    edit = make(everything)
    image = _grab(edit)
    assert edit.covers, "a lean run produces cover rectangles"
    cover = edit.covers[0]

    def inside(x, y):
        return cover.left() <= x < cover.right() and cover.top() <= y < cover.bottom()

    ghost_inside = [p for p in _ink_points(image, _reddish) if inside(*p)]
    base_inside = [p for p in base_red if inside(*p)]
    white_inside = [p for p in _ink_points(image, whiteish) if inside(*p)]
    assert base_inside and white_inside
    assert len(ghost_inside) / len(base_inside) < 0.03
    shift = _upper_lower_shift(white_inside)
    assert shift is not None and shift < -0.15  # still leaning left when selected

    # partial selection covering only the run: the whole cover is filled with Highlight
    edit = make((run_lo, run_hi))
    image = _grab(edit)
    assert len(edit.covers) == 1
    only = edit.covers[0]
    region = [(x, y) for x in range(int(only.left()) + 1, int(only.right()) - 1) for y in range(int(only.top()) + 1, int(only.bottom()) - 1)]
    blues = sum(1 for x, y in region if image.pixelColor(x, y) == BLUE)
    assert blues / len(region) > 0.5, "the selected run is filled with the Highlight colour"

    # a selection ending in the middle of the run splits it into a selected and an unselected piece
    mid = run_lo + 2
    edit = make((run_lo, mid))
    image = _grab(edit)
    assert len(edit.covers) == 2
    selected_cover, plain_cover = edit.covers
    assert selected_cover.right() == pytest.approx(plain_cover.left(), abs=1.01)

    def fraction(rect, color):
        pts = [(x, y) for x in range(int(rect.left()) + 1, int(rect.right()) - 1) for y in range(int(rect.top()) + 1, int(rect.bottom()) - 1)]
        return sum(1 for x, y in pts if image.pixelColor(x, y) == color) / len(pts)

    assert fraction(selected_cover, BLUE) > 0.5
    assert fraction(plain_cover, WHITE) > 0.5



def test_lean_redraw_keeps_its_font_size_on_a_painter_with_a_different_default(qapp):
    """Regression: the redraw came out about a quarter of the size of the text it covered.

    `piece.font` comes from `fmt.font().resolve(default)`: the size is filled in but the RESOLVE MASK is
    empty, and `QPainter.setFont` re-resolves such a font against the painter's own current font. The live
    editor's viewport painter hides that (its default IS the widget font); a plain `QImage` painter, which
    is what the Embed snapshot uses, defaults to 9pt, so a 30pt run was redrawn at 9pt. Paint onto exactly
    such a painter and require the sheared ink to be about as tall as the line it belongs to.
    """
    edit = _make_edit(_model([Run("MMMM", LEAN)]), point_size=30, draw_overlay=False)
    _grab(edit)  # force a real layout: collect_segments needs one
    clip = QRectF(edit.viewport().rect())
    segments = collect_segments(edit.document(), QPointF(0.0, 0.0), clip)
    assert segments and segments[0].lean, "precondition: the run is a left-lean segment"
    seg = segments[0]
    assert seg.font.pointSizeF() == pytest.approx(30.0), "precondition: the segment carries the 30pt font"

    image = QImage(edit.width(), edit.height(), QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(WHITE)
    painter = QPainter(image)
    try:
        assert painter.font().pointSizeF() != pytest.approx(30.0), "precondition: the painter default differs"
        paint_segments(painter, segments, palette=_palette(text=BLACK))
    finally:
        painter.end()
    ys = [y for x, y, r, g, b in _pixels(image.convertToFormat(QImage.Format.Format_ARGB32)) if _dark(r, g, b)]
    if not ys:
        pytest.skip("platform drew no text ink")
    assert max(ys) - min(ys) + 1 >= 0.6 * seg.line_h, "the redraw must be about as tall as the text it covers"
