"""Embed / Un-embed for the SAP Modifier: snapshot rendering and checked PNG handling (hazard C).

An Embed replaces one paragraph's selected text with an inline PICTURE of that
text while the ORIGINAL TEXT is kept underneath (`EmbedRun.source`); Un-embed puts
the text back. The picture is display-only: the tree, the counts, Run and the
plain-text export all read `source`. This module owns the whole pipeline:

    render_range_snapshot(editor, lo, hi) -> QImage      draw the text, no editing
    encode_png(image) -> bytes                           in-memory PNG
    decode_png_checked(png_bytes) -> QImage              header first, then decode
    embed_range(editor, lo, hi, embeds) -> str           text -> picture (one undo step)
    unembed_ranges(editor, embeds) -> int                picture -> text (one undo step)

plus small helpers the editor uses (`embed_target_range`, `embed_image_format`,
`embed_image_for_url`, `find_embed_positions`). `editor` is DUCK-TYPED: anything
with `document()`, `textCursor()`, `setTextCursor()`, `palette()` and `font()`
works (a plain `QTextEdit` in the tests, `WriteEditor` in the app). This module
never imports `write_editor`; the dependency runs write_embed -> write_docmodel,
write_overlay, write_model.

Snapshot rendering (decision D8: DPR 1, the run's own formatting, no live syntax
colours - so the file is deterministic and resolution independent, and blurry on a
HiDPI screen). `[lo, hi)` is copied into a fresh `QTextDocument` through a
`QTextDocumentFragment` (the user's document is never touched while rendering),
the block indent is reset, the default font is the editor's, the margin is
`SNAPSHOT_MARGIN` and the text is
laid out unwrapped (`setTextWidth(-1)`, `idealWidth()`) and only wrapped at
`SNAPSHOT_MAX_W`. A picture over `SNAPSHOT_MAX_H` or `SNAPSHOT_MAX_PIXELS` is
REFUSED (`EmbedRefused`) before any pixel is allocated. The image is
`ARGB32_Premultiplied`, filled with the palette Base colour, painted with
`documentLayout().draw()` and then the SAME overlay painter the editor uses
(`write_overlay.paint_overlays`), so Dash diagonals and left-leaning runs are in
the picture. No highlighter is ever attached to the snapshot document, and the
`QPainter` is always ended in a `finally`.

Decompression-bomb defence (risk 5): a PNG is only ever decoded through
`decode_png_checked`, whose FIRST step reads the dimensions from the IHDR bytes
(Qt-free, `write_format.check_png`) and refuses a picture over the caps before an
image library is allowed near the data; only then does a `QImageReader` over an
in-memory buffer confirm the same size, and only then `read()` decodes.
`QImageReader.setAllocationLimit` is NEVER called (it is process-global). There is
ONE implementation of that decode: `write_docmodel.decode_embed_png`, which
`model_to_document` needs and which this module cannot be imported by; the public
`decode_png_checked` here delegates to it (through the module attribute, so a spy
sees the real call). A corrupt picture never crashes anything: on load it degrades
to its source text plus a warning (`model_to_document`), and the resource loader
`embed_image_for_url` answers None.

PySide ownership trap (found twice, with probes): `QTextDocument.clone()` returns a
wrapper Python does NOT own, and the binding keeps it registered against the
original document even after `del` + `gc.collect()`, so simply dropping it leaks
one whole document per snapshot. Deleting it by hand (`shiboken6.delete`) is worse:
the binding still lists the stale wrapper, and when the EDITOR's own document is
destroyed later shiboken tries to clean it up and the process dies with an access
violation (exit 139), long after the embed itself looked fine. So `clone()` is not
used at all: the snapshot document is a plain `QTextDocument()` filled from a
`QTextDocumentFragment` of just the range. Python owns it, refcounting frees it when
the render returns, nothing is deleted by hand, and only the selection is copied
(not the whole document). Image resources the range needs are copied across.

Documented choices (the plan leaves them open):
- D4 - an embed is refused for a multi-paragraph range; only the "select a whole
  line" gesture (a selection that ends exactly at the start of the next paragraph)
  is trimmed to its own paragraph, by `embed_target_range`, never by `embed_range`.
- No selection means the whole current paragraph (`embed_target_range`); a
  paragraph that already holds a picture is then refused (no nested embeds).
- `SNAPSHOT_MAX_CHARS` is an extra safety valve: a range longer than that is refused
  before the layout even starts, so a megabyte paragraph cannot freeze the UI.
- `unembed_ranges` takes the registry as its second argument (`embeds`); a
  non-empty selection that holds no embed falls through to the caret rules, as in
  the plan; an embed with no source text (a hand-made file) is left in place.
- The caret ends just after the picture (Embed) or just after the restored text
  (Un-embed); the typing format is reset after an Embed so text typed next is plain.

Explicitly out of scope: the editor's toolbar wiring and undo bookkeeping beyond
one edit block, clipboard handling, and file I/O (`write_format.py`).
"""

import math

from PySide6.QtCore import QBuffer, QIODevice, QPointF, QRectF, QUrl
from PySide6.QtGui import (
    QAbstractTextDocumentLayout,
    QImage,
    QPainter,
    QPalette,
    QTextBlockFormat,
    QTextCharFormat,
    QTextCursor,
    QTextDocument,
    QTextDocumentFragment,
    QTextImageFormat,
    QTextOption,
)

import write_docmodel
from write_docmodel import (
    EMBED_URL_SCHEME,
    OBJECT_CHAR,
    PROP_EMBED,
    block_fragments,
    char_format_from_tags,
    embed_id_of,
    is_embed_format,
    runs_in_range,
)
from write_model import MAX_EMBED_PNG_BYTES, MAX_RUNS_PER_PARAGRAPH, sanitize_text
from write_overlay import paint_overlays

SNAPSHOT_MAX_W = 2000
SNAPSHOT_MAX_H = 1000
SNAPSHOT_MAX_PIXELS = 1_500_000
SNAPSHOT_MARGIN = 2
SNAPSHOT_MAX_CHARS = 50_000

UNEMBED_HINT = "Place the cursor next to an embed (or select it)."


class EmbedRefused(Exception):
    """An Embed / Un-embed request that will not be carried out; `str(exc)` is a status-bar message.

    Raised BEFORE anything is changed: the document, its undo stack and the embed
    registry are exactly as they were.
    """


# -- PNG in / out ------------------------------------------------------------------


def encode_png(image: QImage) -> bytes:
    """Encode `image` as PNG bytes in memory (a `QBuffer`; nothing touches the disk).

    Raises `ValueError` if Qt cannot encode it.
    """
    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    try:
        saved = image.save(buffer, "PNG")
        data = bytes(buffer.data())
    finally:
        buffer.close()
    if not saved or not data:
        raise ValueError("the picture could not be encoded as PNG")
    return data


def decode_png_checked(png_bytes: bytes) -> QImage:
    """Decode a PNG to a `QImage`, refusing anything suspicious before any pixel is allocated.

    Order: the IHDR dimensions and the caps (`MAX_IMAGE_DIM`, `MAX_IMAGE_PIXELS`)
    from the header bytes alone; a `QImageReader` over an in-memory buffer whose
    reported size must equal the header's; then `read()`. Raises `ValueError` for
    every failure, a null image included. Never calls
    `QImageReader.setAllocationLimit`. This is the ONE decode implementation: it
    delegates to `write_docmodel.decode_embed_png` (see the module docstring).
    """
    return write_docmodel.decode_embed_png(png_bytes)


# -- snapshot -----------------------------------------------------------------------


def _checked_range(document: QTextDocument, lo: int, hi: int) -> tuple[int, int]:
    """Order `lo`/`hi`, and refuse an empty, out-of-document or absurdly long range."""
    lo, hi = min(lo, hi), max(lo, hi)
    if lo < 0 or hi > document.characterCount() - 1:
        raise EmbedRefused("The selection is outside the document.")
    if hi == lo:
        raise EmbedRefused("Nothing to embed: the selection is empty.")
    if hi - lo > SNAPSHOT_MAX_CHARS:
        raise EmbedRefused(f"That selection is too long to embed ({hi - lo} characters; the limit is {SNAPSHOT_MAX_CHARS}).")
    return lo, hi


def _snapshot_document(document: QTextDocument, lo: int, hi: int) -> QTextDocument:
    """A fresh, Python-owned document holding a copy of `[lo, hi)` (see the module docstring's ownership trap).

    The copy goes through a `QTextDocumentFragment`, so char formats (bold, the Dash and
    lean properties, spacing) travel with the text and the user's document is only read.
    Any image resource a picture inside the range needs is copied across, since a fragment
    carries the resource NAME, not its bytes. Whatever block format the copied text
    inherited (an indent, most of all) is not part of the picture, so every block is reset
    to the default.
    """
    source = QTextCursor(document)
    source.setPosition(lo)
    source.setPosition(hi, QTextCursor.MoveMode.KeepAnchor)
    snap = QTextDocument()
    snap.setUndoRedoEnabled(False)
    QTextCursor(snap).insertFragment(QTextDocumentFragment(source))
    block = snap.begin()
    while block.isValid():
        for fragment in block_fragments(block):
            fmt = fragment.charFormat()
            if fmt.isImageFormat():
                url = QUrl(fmt.toImageFormat().name())
                data = document.resource(QTextDocument.ResourceType.ImageResource, url)
                if data is not None:
                    snap.addResource(QTextDocument.ResourceType.ImageResource, url, data)
        block = block.next()
    cursor = QTextCursor(snap)
    cursor.select(QTextCursor.SelectionType.Document)
    cursor.setBlockFormat(QTextBlockFormat())
    return snap


def _draw_snapshot(snap: QTextDocument, font, palette: QPalette) -> QImage:
    """Lay out the snapshot document, refuse it if it is too big, and paint it into a new image."""
    snap.setDefaultFont(font)
    snap.setDocumentMargin(SNAPSHOT_MARGIN)
    option = snap.defaultTextOption()
    option.setWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
    snap.setDefaultTextOption(option)
    snap.setTextWidth(-1)
    ideal = snap.idealWidth()
    snap.setTextWidth(min(SNAPSHOT_MAX_W, max(1, math.ceil(ideal))))
    size = snap.size()
    width = max(1, math.ceil(size.width()))
    height = max(1, math.ceil(size.height()))
    if width > SNAPSHOT_MAX_W or height > SNAPSHOT_MAX_H or width * height > SNAPSHOT_MAX_PIXELS:
        raise EmbedRefused(
            f"That text would make a {width} x {height} picture; the limits are {SNAPSHOT_MAX_W} x {SNAPSHOT_MAX_H} "
            f"and {SNAPSHOT_MAX_PIXELS} pixels. Select less text."
        )
    image = QImage(width, height, QImage.Format.Format_ARGB32_Premultiplied)
    if image.isNull():
        raise EmbedRefused("There is not enough memory to draw that picture.")
    image.setDevicePixelRatio(1.0)
    image.fill(palette.color(QPalette.ColorRole.Base))
    clip = QRectF(0.0, 0.0, float(width), float(height))
    painter = QPainter(image)
    try:
        context = QAbstractTextDocumentLayout.PaintContext()
        context.palette = palette
        context.clip = clip
        snap.documentLayout().draw(painter, context)
        paint_overlays(painter, snap, QPointF(0.0, 0.0), clip, palette, None)
    finally:
        painter.end()
    return image


def render_range_snapshot(editor, lo: int, hi: int) -> QImage:
    """Render document positions `[lo, hi)` of `editor` as a picture (DPR 1, the run's own formatting).

    Does not modify the editor's document. The range may span paragraphs (only
    `embed_range` restricts that). Raises `EmbedRefused` for an empty or
    out-of-document range, a range over `SNAPSHOT_MAX_CHARS`, or a picture over
    `SNAPSHOT_MAX_W` / `SNAPSHOT_MAX_H` / `SNAPSHOT_MAX_PIXELS`. Colours come from
    `editor.palette()` (Active group): Base fills the image, Text draws the words.
    """
    document = editor.document()
    lo, hi = _checked_range(document, lo, hi)
    palette = QPalette(editor.palette())
    palette.setCurrentColorGroup(QPalette.ColorGroup.Active)
    font = editor.font()
    # Python-owned and freed by refcount when this returns (or raises): never delete it by hand.
    snap = _snapshot_document(document, lo, hi)
    return _draw_snapshot(snap, font, palette)


# -- embed --------------------------------------------------------------------------


def embed_image_format(embed_id: str, width: int, height: int) -> QTextImageFormat:
    """The image format of an embed: resource name `mem://<id>`, its size, and `PROP_EMBED = id`."""
    fmt = QTextImageFormat()
    fmt.setName(f"{EMBED_URL_SCHEME}://{embed_id}")
    fmt.setWidth(width)
    fmt.setHeight(height)
    fmt.setProperty(PROP_EMBED, embed_id)
    return fmt


def embed_target_range(editor) -> tuple[int, int]:
    """The `(lo, hi)` an Embed acts on: the selection, else the whole current paragraph.

    A selection that starts in one paragraph and ends exactly at the start of the
    next (what "select the whole line" gestures produce) is trimmed to the end
    of its own paragraph, since it selects no text of the next one. Anything else
    is returned as the user made it, so a genuine multi-paragraph selection is
    still refused by `embed_range` (decision D4).
    """
    document = editor.document()
    cursor = editor.textCursor()
    if cursor.hasSelection():
        lo, hi = cursor.selectionStart(), cursor.selectionEnd()
        first = document.findBlock(lo)
        last = document.findBlock(hi)
        if last.blockNumber() == first.blockNumber() + 1 and hi == last.position():
            hi = first.position() + first.length() - 1
        return lo, hi
    block = cursor.block()
    return block.position(), block.position() + block.length() - 1


def embed_range(editor, lo: int, hi: int, embeds) -> str:
    """Replace `[lo, hi)` with a picture of it, keeping the text underneath; return the embed id.

    `embeds` is the session `EmbedRegistry` (the caller keeps it: undo must always
    find its picture). Refuses with `EmbedRefused` - and changes nothing - when the
    range is empty or only whitespace, spans paragraphs (D4), contains a picture
    (no nested embeds), has more than `MAX_RUNS_PER_PARAGRAPH` formatting runs, is
    too big to render, encodes to more than `MAX_EMBED_PNG_BYTES`, or the registry
    is full. Otherwise: one edit block on one cursor (one undo step) that removes
    the range, inserts the image (`mem://<id>` resource registered on the document,
    `PROP_EMBED` set) and resets the typing format so text typed next does not
    inherit the embed property (V7); the editor's caret is left after the picture.
    """
    document = editor.document()
    lo, hi = _checked_range(document, lo, hi)
    if document.findBlock(lo).blockNumber() != document.findBlock(hi).blockNumber():
        raise EmbedRefused("Embed works on one paragraph at a time: select text inside a single paragraph.")
    probe = QTextCursor(document)
    probe.setPosition(lo)
    probe.setPosition(hi, QTextCursor.MoveMode.KeepAnchor)
    selected = probe.selectedText()  # Qt does the UTF-16 slicing; a picture shows up as U+FFFC
    if OBJECT_CHAR in selected:
        raise EmbedRefused("The selection already contains a picture; un-embed it first (embeds cannot be nested).")
    if not selected.strip():
        raise EmbedRefused("Nothing to embed: the selection is only spaces.")
    source = runs_in_range(document, lo, hi)
    if not source:
        raise EmbedRefused("Nothing to embed: the selection is empty.")
    if len(source) > MAX_RUNS_PER_PARAGRAPH:
        raise EmbedRefused(f"That selection has too many formatting changes to embed (limit {MAX_RUNS_PER_PARAGRAPH}).")
    image = render_range_snapshot(editor, lo, hi)
    png = encode_png(image)
    if len(png) > MAX_EMBED_PNG_BYTES:
        raise EmbedRefused(f"The picture would be {len(png)} bytes; the limit is {MAX_EMBED_PNG_BYTES}. Select less text.")
    try:
        embed_id = embeds.add(png, source)
    except ValueError as exc:
        raise EmbedRefused(f"Cannot embed: {exc}.") from None
    fmt = embed_image_format(embed_id, image.width(), image.height())
    document.addResource(QTextDocument.ResourceType.ImageResource, QUrl(fmt.name()), image)
    cursor = editor.textCursor()
    cursor.beginEditBlock()
    try:
        cursor.setPosition(lo)
        cursor.setPosition(hi, QTextCursor.MoveMode.KeepAnchor)
        cursor.removeSelectedText()
        cursor.insertImage(fmt)
        cursor.setCharFormat(QTextCharFormat())  # V7: typed text must not inherit the image's property
    finally:
        cursor.endEditBlock()
    editor.setTextCursor(cursor)
    return embed_id


# -- un-embed -----------------------------------------------------------------------


def find_embed_positions(document: QTextDocument, lo: int, hi: int) -> list[int]:
    """Positions in `[lo, hi)` whose character is an embed picture, ascending.

    An image fragment can hold several merged pictures (V7), one position each.
    A picture is recognised by `isImageFormat()` AND `PROP_EMBED` together.
    """
    found: list[int] = []
    if hi <= lo:
        return found
    block = document.findBlock(max(lo, 0))
    while block.isValid() and block.position() < hi:
        for frag in block_fragments(block):
            if not is_embed_format(frag.charFormat()):
                continue
            start = frag.position()
            for position in range(start, start + frag.length()):
                if lo <= position < hi:
                    found.append(position)
        block = block.next()
    return found


def _unembed_targets(editor) -> list[int]:
    """Which pictures an Un-embed acts on, in the plan's order; raises `EmbedRefused` if none."""
    document = editor.document()
    cursor = editor.textCursor()
    if cursor.hasSelection():
        inside = find_embed_positions(document, cursor.selectionStart(), cursor.selectionEnd())
        if inside:
            return inside
    position = cursor.position()
    before = find_embed_positions(document, position - 1, position)
    if before:
        return before
    after = find_embed_positions(document, position, position + 1)
    if after:
        return after
    block = cursor.block()
    sole = find_embed_positions(document, block.position(), block.position() + block.length() - 1)
    if len(sole) == 1:
        return sole
    raise EmbedRefused(UNEMBED_HINT)


def _embed_id_at(document: QTextDocument, position: int) -> str | None:
    """The embed id of the picture character at `position` (the format BEFORE a cursor one further on)."""
    probe = QTextCursor(document)
    probe.setPosition(position + 1)
    return embed_id_of(probe.charFormat())


def unembed_ranges(editor, embeds) -> int:
    """Turn pictures back into their source text; return how many were restored.

    Targets, in order: the pictures inside a non-empty selection; else the picture
    just before the caret, then the one just after it; else the sole picture in the
    caret's paragraph; else `EmbedRefused` with `UNEMBED_HINT`. They are processed
    LAST to FIRST inside ONE edit block on one cursor (one undo step), each picture
    replaced by `rec.source` re-inserted run by run with `char_format_from_tags`, so
    bold/line/dash/lean/spread all come back. A picture the registry does not know,
    or one with no source text (a hand-made file), is left in place; if none can be
    restored `EmbedRefused` is raised and nothing changed. The caret ends after the
    last restored text. Never decodes a PNG, so a corrupt one cannot hurt it.
    """
    document = editor.document()
    targets = _unembed_targets(editor)
    cursor = editor.textCursor()
    restored = 0
    caret = None
    cursor.beginEditBlock()
    try:
        for position in reversed(targets):
            embed_id = _embed_id_at(document, position)
            record = embeds.get(embed_id) if embed_id else None
            source = [(sanitize_text(run.text), run.tags) for run in (record.source if record else [])]
            source = [(text, tags) for text, tags in source if text]
            if not source:
                continue
            cursor.setPosition(position)
            cursor.setPosition(position + 1, QTextCursor.MoveMode.KeepAnchor)
            cursor.removeSelectedText()
            for text, tags in source:
                cursor.insertText(text, char_format_from_tags(tags))
            inserted = cursor.position() - position
            # Everything above `position` moves by (inserted - 1) once a LOWER picture is restored later.
            caret = cursor.position() if caret is None else caret + inserted - 1
            restored += 1
    finally:
        cursor.endEditBlock()
    if not restored:
        raise EmbedRefused("That embed has no source text to restore.")
    cursor.setPosition(caret)
    editor.setTextCursor(cursor)
    return restored


# -- load path ----------------------------------------------------------------------


def embed_image_for_url(embeds, url) -> QImage | None:
    """The picture for an embed resource URL (`mem://<id>`), or None - for `QTextEdit.loadResource`.

    None for any other scheme, an id the registry does not hold, or a picture that
    fails the checked decode: the caller then draws nothing rather than reaching
    for the network or the disk.
    """
    text = QUrl(url).toString() if not isinstance(url, str) else url
    prefix = f"{EMBED_URL_SCHEME}://"
    if not text.startswith(prefix):
        return None
    record = embeds.get(text[len(prefix) :])
    if record is None:
        return None
    try:
        return decode_png_checked(record.png)
    except ValueError:
        return None
