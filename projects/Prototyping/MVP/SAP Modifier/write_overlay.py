"""Overlay painting for SAP Modifier: the Dash diagonal and the left lean (hazards A, 2, 3).

Qt has no "diagonal strike" and no "italic the other way", so after
`QTextEdit.paintEvent` has drawn the text normally, this module paints the two
custom looks on top. It is free functions on purpose - the editor's viewport and
the embed snapshot renderer (`write_embed.py`) share the exact same code:

    collect_segments(doc, offset, clip)  -> [RunSegment]   geometry only
    paint_segments(painter, segments, *, palette, selection) -> [QRectF]
    paint_overlays(painter, doc, offset, clip, palette, selection=None)

`offset` is the document-to-painter translation (`QPointF(-hbar, -vbar)` for a
scrolled viewport), `clip` the painter-space rectangle worth painting, `palette`
a `QPalette` whose CURRENT colour group the caller has already chosen
(`setCurrentColorGroup(Active if isActiveWindow() else Inactive)`), and
`selection` `None` or `(start, end)` absolute document positions.

Scheme - cover and redraw. For every visual-line piece of a left-lean run the
original upright glyphs are first covered with a rectangle of the palette Base
colour (Highlight for the selected sub-range), then the text is drawn again with
`QTransform.shear(LEFT_LEAN_SHEAR, 0)`. The base glyphs are NOT made transparent
(a spike showed a highlighter colour resurrecting "hidden" glyphs), so neither
highlighter colours nor selection can bring the upright text back. Pieces are
split at the selection edges and at every syntax-highlighter `FormatRange` edge
(`block.layout().formats()`), so keyword colours survive inside a tilted run and
a selected part is redrawn in HighlightedText on Highlight. A Dash piece gets a
one-pixel diagonal from its bottom-left to its top-right corner, drawn last in
the palette Text colour. The functions return the cover rectangles so the editor
can redraw the caret the cover erased.

Shear direction (fact V4, settled by `test_write_overlay.py`'s pixel test):
`LEFT_LEAN_SHEAR = +0.30`. With Qt's `QTransform.shear(sh, 0)` a POSITIVE `sh`
moves points above the baseline (negative y) to the LEFT, so the top of each
glyph leans left; the negative value leans RIGHT. The pixel test compares the
ink centroid of the upper band against the lower band and against an upright
control, so the sign is proven, not assumed; the constant stays +0.30.

Native-crash guards (each mandatory - a violation is a segfault, not an
exception; `QTextLayout.lineAt` on a block that was never laid out crashes the
process): the document layout must exist; a null/empty `blockBoundingRect` means
"not laid out" and stops the WHOLE collection (that call is also what forces a
lazy layout of the block); `block.layout()` must exist and have `lineCount() > 0`
(`QTextLayout` has no `isValid()`) else the block is skipped; `lineAt(i)` is only
called for `i < lineCount()` and the line must be `isValid()`; the first visible
block is found by BINARY SEARCH over `findBlockByNumber` comparing block-rect
bottoms (never a scan from block 0, never `cursorForPosition`), and collection
stops once a block starts below the clip; `QTextLine.cursorToX` returns a tuple
so a helper takes element 0; the run font is
`fmt.font().resolve(doc.defaultFont())` so bold, strike-out and absolute
letter-spacing carry over. New geometry added later must keep these guards.

Documented limits: left-to-right text only; no per-glyph tilt inside ligatures
(a piece is drawn as one string, cut at tabs so tab stops keep their width);
cover rectangles are the pieces' advance boxes rounded outward, so ink that
overhangs its advance box can survive as a hairline; the highlighter's underline
is redrawn as a plain straight underline (wavy/dotted styles are not
reproduced) and a document-format foreground colour is not carried into the
redraw (the Write model has no colour tag, only the highlighter colours);
`invisible` blocks stop the collection (the app never hides blocks).

Explicitly out of scope: event handling, the caret redraw (the editor's job),
and anything that mutates the document.
"""

import math
from dataclasses import dataclass

from PySide6.QtCore import QPointF, QRectF
from PySide6.QtGui import QColor, QFont, QPainter, QPalette, QPen, QTextBlock, QTextCharFormat, QTextFormat, QTransform

from write_docmodel import block_fragments, is_dash, is_left_lean

LEFT_LEAN_SHEAR = 0.30
_ALL_FONT_PROPERTIES_RESOLVED = 0x7FFFFFFF  # QFont::AllPropertiesResolved (PySide6 does not export the name)


@dataclass
class RunSegment:
    """One visual-line piece of a Dash and/or left-lean fragment.

    `block` is the `QTextBlock`; `lo`/`hi` are BLOCK-relative character offsets
    (`hi` exclusive; add `block.position()` for absolute positions). `line_top`,
    `line_h` and `baseline` are painter-space y values (the offset already
    applied), `x0`/`x1` the painter-space x extent of the piece, `text` the piece's
    characters, `font` the resolved run font. `dash` / `lean` say which look the
    piece wants; a fragment that is both yields one segment with both flags.
    """

    block: QTextBlock
    lo: int
    hi: int
    line_top: float
    line_h: float
    baseline: float
    x0: float
    x1: float
    text: str
    font: QFont
    dash: bool
    lean: bool


@dataclass
class _Piece:
    """A lean sub-piece after splitting (selection / highlighter edges / tabs)."""

    text: str
    x0: float
    x1: float
    line_top: float
    line_h: float
    baseline: float
    font: QFont
    selected: bool
    color: QColor | None
    underline: bool


def _cursor_x(line, pos: int) -> float:
    """`QTextLine.cursorToX` returns `(x, cursorPos)` - take the x."""
    result = line.cursorToX(pos)
    if isinstance(result, (tuple, list)):
        result = result[0]
    return float(result)


def _rect_unusable(rect: QRectF) -> bool:
    """True for a null / zero-height rect: the block has not been laid out (V2)."""
    return rect.isNull() or not (rect.height() > 0.0)


def _first_visible_block(doc, layout, doc_top: float):
    """The first block whose bottom is at or below `doc_top`, by binary search; else None.

    Block rects are monotonic in y (a plain vertical stack), so bisecting over
    `findBlockByNumber` needs O(log n) rect queries instead of a scan from block 0
    (hazard P2). A null rect anywhere means the document is not laid out: return
    None so nothing is collected.
    """
    lo, hi = 0, doc.blockCount() - 1
    found = None
    while lo <= hi:
        mid = (lo + hi) // 2
        block = doc.findBlockByNumber(mid)
        if not block.isValid():
            return None
        rect = layout.blockBoundingRect(block)
        if _rect_unusable(rect):
            return None
        if rect.bottom() < doc_top:
            lo = mid + 1
        else:
            found = block
            hi = mid - 1
    return found


def collect_segments(doc, offset, clip) -> list:
    """Return the `RunSegment`s of every Dash / left-lean piece that intersects `clip`.

    Geometry only, no painting, no document mutation. Returns `[]` for a document
    that has not been laid out (never shown, or hidden and resized) - see the
    guards in the module docstring. Only the visible blocks are visited, and
    within a block only the visual lines that touch the clip.
    """
    segments: list = []
    layout = doc.documentLayout()
    if layout is None:
        return segments
    offset = QPointF(offset)
    clip = QRectF(clip)
    block = _first_visible_block(doc, layout, clip.top() - offset.y())
    if block is None:
        return segments
    doc_font = doc.defaultFont()
    while block.isValid():
        rect = layout.blockBoundingRect(block)
        if _rect_unusable(rect):
            break
        if rect.top() + offset.y() > clip.bottom():
            break
        _collect_block(block, rect, offset, clip, doc_font, segments)
        block = block.next()
    return segments


def _collect_block(block, rect: QRectF, offset: QPointF, clip: QRectF, doc_font: QFont, out: list) -> None:
    text_layout = block.layout()
    if text_layout is None or text_layout.lineCount() <= 0:
        return
    # The layout origin in painter space. `blockBoundingRect` is the layout's bounding rect MOVED to
    # the layout position, so its top-left corner IS that origin. `QTextLine.x()` and `cursorToX`
    # already carry the block indent (Qt bakes it into the line x, not into the layout position),
    # so nothing may be subtracted here: subtracting the layout's own bounding corner would cancel
    # the indent and put every cover, redraw and Dash `indent * indentWidth` px left of the text.
    origin_x = rect.left() + offset.x()
    origin_y = rect.top() + offset.y()
    line_count = text_layout.lineCount()
    block_text = block.text()
    block_start = block.position()
    for frag in block_fragments(block):
        fmt = frag.charFormat()
        dash = is_dash(fmt)
        lean = is_left_lean(fmt)
        if not (dash or lean):
            continue
        lo = frag.position() - block_start
        hi = lo + frag.length()
        if hi <= lo:
            continue
        font = fmt.font().resolve(doc_font)
        for i in range(line_count):
            line = text_layout.lineAt(i)
            if not line.isValid():
                continue
            start = max(lo, line.textStart())
            end = min(hi, line.textStart() + line.textLength())
            if end <= start:
                continue
            line_top = origin_y + line.y()
            if line_top > clip.bottom() or line_top + line.height() < clip.top():
                continue
            out.append(
                RunSegment(
                    block,
                    start,
                    end,
                    line_top,
                    line.height(),
                    line_top + line.ascent(),
                    origin_x + _cursor_x(line, start),
                    origin_x + _cursor_x(line, end),
                    block_text[start:end],
                    font,
                    dash,
                    lean,
                )
            )


def _highlight_at(formats, a: int, b: int):
    """`(colour, underline)` of the highlighter format covering `[a, b)`, or `(None, False)`."""
    for item in reversed(formats):
        if item.start <= a and b <= item.start + item.length:
            fmt = item.format
            color = fmt.foreground().color() if fmt.hasProperty(QTextFormat.Property.ForegroundBrush) else None
            underline = fmt.underlineStyle() != QTextCharFormat.UnderlineStyle.NoUnderline
            return color, underline
    return None, False


def _lean_pieces(seg: RunSegment, selection) -> list:
    """Split a lean segment at selection edges, highlighter-format edges and tabs."""
    text_layout = seg.block.layout()
    if text_layout is None or text_layout.lineCount() <= 0:
        return []
    line = text_layout.lineForTextPosition(seg.lo)
    if not line.isValid():
        return []
    block_start = seg.block.position()
    cuts = {seg.lo, seg.hi}
    if selection is not None:
        for edge in selection:
            if seg.lo < edge - block_start < seg.hi:
                cuts.add(edge - block_start)
    formats = text_layout.formats()
    for item in formats:
        for edge in (item.start, item.start + item.length):
            if seg.lo < edge < seg.hi:
                cuts.add(edge)
    for index, char in enumerate(seg.text):
        if char == "\t":  # cut around tabs: drawText would not honour the layout's tab stop
            cuts.add(seg.lo + index)
            cuts.add(seg.lo + index + 1)
    ordered = sorted(cuts)
    origin_x = seg.x0 - _cursor_x(line, seg.lo)
    pieces = []
    for a, b in zip(ordered, ordered[1:]):
        selected = selection is not None and selection[0] <= block_start + a and block_start + b <= selection[1]
        color, underline = _highlight_at(formats, a, b)
        pieces.append(
            _Piece(
                seg.text[a - seg.lo : b - seg.lo],
                origin_x + _cursor_x(line, a),
                origin_x + _cursor_x(line, b),
                seg.line_top,
                seg.line_h,
                seg.baseline,
                seg.font,
                selected,
                color,
                underline,
            )
        )
    return pieces


def _aligned(x0: float, y0: float, x1: float, y1: float) -> QRectF:
    """A rectangle rounded OUTWARD to whole pixels, so a cover leaves no half-covered edge."""
    left, top = math.floor(x0), math.floor(y0)
    right, bottom = math.ceil(x1), math.ceil(y1)
    return QRectF(left, top, max(0, right - left), max(0, bottom - top))


def paint_segments(painter: QPainter, segments, *, palette: QPalette, selection=None) -> list:
    """Paint the overlays for `segments`; return the cover rectangles (lean pieces only).

    Three passes so an earlier piece's tilted overhang is never wiped by a later
    piece's cover: covers, then sheared text, then Dash diagonals. The painter's
    state is saved and restored, and its existing transform is respected.
    """
    covers: list = []
    if not segments:
        return covers
    if selection is not None:
        selection = tuple(sorted(selection))
        if selection[0] == selection[1]:
            selection = None
    pieces: list = []
    for seg in segments:
        if seg.lean:
            pieces.extend(_lean_pieces(seg, selection))
    base = palette.color(QPalette.ColorRole.Base)
    highlight = palette.color(QPalette.ColorRole.Highlight)
    text_color = palette.color(QPalette.ColorRole.Text)
    highlighted_text = palette.color(QPalette.ColorRole.HighlightedText)
    painter.save()
    try:
        for piece in pieces:
            rect = _aligned(piece.x0, piece.line_top, piece.x1, piece.line_top + piece.line_h)
            if rect.width() <= 0 or rect.height() <= 0:
                continue
            painter.fillRect(rect, highlight if piece.selected else base)
            covers.append(rect)
        for piece in pieces:
            if not piece.text.strip("\t"):
                continue  # only tabs: nothing to draw (the cover already blanked it)
            font = QFont(piece.font)
            # `piece.font` was built with `fmt.font().resolve(default)`, which leaves the resolve mask
            # EMPTY even though the size/family are filled in. `QPainter.setFont` re-resolves a font
            # against the painter's current one, so on a painter whose default differs from the
            # document's (a fresh QImage painter: 9pt) the size silently fell back and the redraw came
            # out tiny. Mark every property resolved so the painter takes the font exactly as given.
            font.setResolveMask(_ALL_FONT_PROPERTIES_RESOLVED)
            font.setItalic(False)  # the tilt IS the italic; a native slant on top would double it
            if piece.underline:
                font.setUnderline(True)
            pen_color = highlighted_text if piece.selected else (piece.color or text_color)
            painter.save()
            transform = QTransform()
            transform.translate(piece.x0, piece.baseline)
            transform.shear(LEFT_LEAN_SHEAR, 0.0)
            painter.setTransform(transform, True)
            painter.setFont(font)
            painter.setPen(pen_color)
            painter.drawText(QPointF(0.0, 0.0), piece.text)
            painter.restore()
        for seg in segments:
            if seg.dash:
                painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
                painter.setPen(QPen(text_color, 1.0))
                painter.drawLine(
                    QPointF(seg.x0, seg.line_top + seg.line_h - 1.0),
                    QPointF(seg.x1, seg.line_top + 1.0),
                )
    finally:
        painter.restore()
    return covers


def paint_overlays(painter: QPainter, doc, offset, clip, palette: QPalette, selection=None) -> list:
    """Collect and paint the Dash / left-lean overlays for `doc`; return the cover rectangles."""
    segments = collect_segments(doc, offset, clip)
    return paint_segments(painter, segments, palette=palette, selection=selection)
