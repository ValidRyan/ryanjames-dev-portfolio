"""Bridge between the pure Write model and a live `QTextDocument` (hazard B).

Qt-using sibling of `write_model.py`: it owns the three custom property ids, the
mapping between the canonical tags and a `QTextCharFormat`, and the two
conversions `document_to_model` / `model_to_document`. The editor
(`write_editor.py`) uses it to load and save, the overlay painter
(`write_overlay.py`) uses `is_dash` / `is_left_lean` to find what to draw, and
the embed pipeline (`write_embed.py`) uses `runs_in_range`, `char_format_from_tags`
and `embed_id_of`. It imports only `write_model` and `write_format` (the Qt-free
PNG header checks), never any other Qt module of the SAP Modifier.

Tag <-> format mapping (one place, so every tool agrees):
- bold -> font weight 700 (read back as bold when weight >= 600);
- line -> strike-out;
- dash -> `PROP_DASH` (bool). Painted by the overlay, Qt has no such thing;
- italic "right" -> native italic; "left" -> `PROP_LEAN = 2` with native italic
  OFF (left lean is custom-painted by the overlay);
- spread -> absolute letter-spacing in px (read back only when the property is
  present, of the Absolute type, and non-zero).
Off-values (weight 400, `PROP_LEAN = 0`, dash False, spacing 0) read back as
"absent": `QTextCursor.mergeCharFormat` cannot delete a property, only overwrite
it, so an "off" tool leaves an explicit off-value behind and the reader must
treat it as no tag at all.

`document_to_model` reads DOCUMENT formats only. Syntax-highlighter formats live
in `QTextLayout.formats()`, an overlay that `QTextFragment.charFormat()` never
sees, so colours can never leak into tags (a test proves it with a highlighter
that sets bold, italic and spacing).

Embeds are image fragments carrying `PROP_EMBED` (the registry id). Two facts
shape the reader and the writer (verified, V7): adjacent identical images MERGE
into a single fragment whose text is `"\\ufffc\\ufffc"`, so one fragment yields
`len(frag.text())` embeds; and text typed right after `insertImage` INHERITS the
image format's custom property, so every insert here is followed by a reset of
the cursor's typing format, and an embed is recognised by `isImageFormat()` AND
the property together (a text fragment never counts).

Documented choice (decoder): `model_to_document` needs a checked PNG decode to
build the inline image. `write_embed.decode_png_checked` is the planned home of
that function but it imports this module, so the same checked decode lives here
as `decode_embed_png` (Qt-free header gate first via `check_png`, then a
`QImageReader` over an in-memory `QBuffer` whose size must match the header) and
`model_to_document` also accepts an injectable `decode=` callable. It NEVER calls
`QImageReader.setAllocationLimit` (process-global). A picture that cannot be
decoded, or a registry that is full, degrades to the embed's source text plus a
warning - the document never loses the words.

Explicitly out of scope: painting (`write_overlay.py`), rendering snapshots
(`write_embed.py`), file I/O (`write_format.py`) and any editor behaviour.
"""

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QUrl
from PySide6.QtGui import (
    QFont,
    QImage,
    QImageReader,
    QSyntaxHighlighter,
    QTextBlockFormat,
    QTextCharFormat,
    QTextCursor,
    QTextDocument,
    QTextFormat,
    QTextImageFormat,
)

from write_format import check_png
from write_model import (
    EmbedRun,
    Paragraph,
    Run,
    WriteDocument,
    clamp_indent,
    coalesce_runs,
    normalize_tags,
    sanitize_text,
)

# Custom property ids. Values are plain ints (`UserProperty` is an enum member).
_USER = int(QTextFormat.UserProperty)
PROP_DASH = _USER + 1
PROP_LEAN = _USER + 2
PROP_EMBED = _USER + 3

LEAN_NONE = 0
LEAN_LEFT = 2
BOLD_WEIGHT = 700
BOLD_READ_THRESHOLD = 600
INDENT_WIDTH_PX = 24.0
OBJECT_CHAR = "\N{OBJECT REPLACEMENT CHARACTER}"
EMBED_URL_SCHEME = "mem"


# -- tags <-> QTextCharFormat ---------------------------------------------------


def set_tag(fmt: QTextCharFormat, key: str, value) -> QTextCharFormat:
    """Set ONE tag on `fmt` explicitly, including its off-value, and return `fmt`.

    `key` is a canonical tag key (`bold`, `line`, `dash`, `italic`, `spread`);
    `value` is the canonical value or a falsy/None off-value. Always writes the
    property (never just removes it) because merging cannot clear one: the
    editor's tools call this so "off" really overwrites an earlier "on". An
    unknown key is ignored. Italic sets the native flag and `PROP_LEAN`
    together so the two can never disagree.
    """
    if key == "bold":
        fmt.setFontWeight(BOLD_WEIGHT if value else 400)
    elif key == "line":
        fmt.setFontStrikeOut(bool(value))
    elif key == "dash":
        fmt.setProperty(PROP_DASH, bool(value))
    elif key == "italic":
        fmt.setFontItalic(value == "right")
        fmt.setProperty(PROP_LEAN, LEAN_LEFT if value == "left" else LEAN_NONE)
    elif key == "spread":
        px = normalize_tags({"spread": value}).get("spread", 0.0) if value else 0.0
        fmt.setFontLetterSpacingType(QFont.SpacingType.AbsoluteSpacing)
        fmt.setFontLetterSpacing(px)
    return fmt


def apply_tags_to_format(fmt: QTextCharFormat, tags) -> QTextCharFormat:
    """Overwrite all five tags on `fmt` from `tags` (absent -> the explicit off-value)."""
    tags = normalize_tags(tags)
    for key in ("bold", "line", "dash", "italic", "spread"):
        set_tag(fmt, key, tags.get(key))
    return fmt


def char_format_from_tags(tags) -> QTextCharFormat:
    """A minimal `QTextCharFormat` for `tags`: only the ON tags are written.

    A fresh format with nothing else in it, so inserting text with it never
    inherits another run's bold or lean. Used for every run in
    `model_to_document` and by Un-embed when it re-inserts the source runs.
    """
    fmt = QTextCharFormat()
    for key, value in normalize_tags(tags).items():
        set_tag(fmt, key, value)
    return fmt


def is_dash(fmt: QTextCharFormat) -> bool:
    """True when `fmt` carries the Dash property (and is not an image)."""
    return (not fmt.isImageFormat()) and fmt.boolProperty(PROP_DASH)


def is_left_lean(fmt: QTextCharFormat) -> bool:
    """True when `fmt` carries the left-lean property (and is not an image)."""
    return (not fmt.isImageFormat()) and fmt.intProperty(PROP_LEAN) == LEAN_LEFT


def tags_from_char_format(fmt: QTextCharFormat) -> dict:
    """The canonical tags of a DOCUMENT character format (see the module docstring).

    Off-values read as absent. Left lean wins over a stray native italic flag
    (the editor never sets both, but a pasted or merged format could). An image
    format has no text tags.
    """
    if fmt.isImageFormat():
        return {}
    tags: dict = {}
    if fmt.fontWeight() >= BOLD_READ_THRESHOLD:
        tags["bold"] = True
    if fmt.fontStrikeOut():
        tags["line"] = True
    if fmt.boolProperty(PROP_DASH):
        tags["dash"] = True
    if fmt.intProperty(PROP_LEAN) == LEAN_LEFT:
        tags["italic"] = "left"
    elif fmt.fontItalic():
        tags["italic"] = "right"
    if fmt.hasProperty(QTextFormat.Property.FontLetterSpacing):
        if fmt.fontLetterSpacingType() == QFont.SpacingType.AbsoluteSpacing:
            spacing = fmt.fontLetterSpacing()
            if spacing != 0.0:
                tags["spread"] = spacing
    return normalize_tags(tags)


def is_embed_format(fmt: QTextCharFormat) -> bool:
    """True for an image format that carries an embed id (image AND property together)."""
    return fmt.isImageFormat() and fmt.hasProperty(PROP_EMBED)


def embed_id_of(fmt: QTextCharFormat) -> str | None:
    """The embed registry id stored on an embed image format, else None."""
    if not is_embed_format(fmt):
        return None
    return fmt.stringProperty(PROP_EMBED) or None


# -- fragment walking ------------------------------------------------------------


def block_fragments(block):
    """Yield the `QTextFragment`s of `block` in order (PySide's iterator has no `__iter__`)."""
    it = block.begin()
    while not it.atEnd():
        yield it.fragment()
        it += 1


def _paragraph_runs(block, embeds) -> list:
    runs: list = []
    for frag in block_fragments(block):
        fmt = frag.charFormat()
        text = frag.text()
        if fmt.isImageFormat():
            embed_id = embed_id_of(fmt)
            if embed_id is None:
                continue  # a foreign image: nothing to keep (its U+FFFC would be scrubbed anyway)
            # V7: identical neighbours merge, so one fragment is N embeds.
            for _ in range(len(text)):
                record = embeds.get(embed_id)
                if record is not None:  # unknown id (cannot happen: the registry is append-only)
                    runs.append(record)
            continue
        clean = sanitize_text(text)
        if clean:
            runs.append(Run(clean, tags_from_char_format(fmt)))
    return runs


def document_to_model(document: QTextDocument, embeds) -> WriteDocument:
    """Read a `QTextDocument` into a `WriteDocument` (document formats only).

    One `Paragraph` per block: `indent` from `blockFormat().indent()` (clamped),
    runs from the fragments, coalesced. Text goes through `sanitize_text` so a
    soft line break or a stray control character can never reach a saved file;
    an embed fragment becomes one `EmbedRun` per U+FFFC it holds, looked up by
    id in `embeds` (an `EmbedRegistry`). Documented choice: an embed whose id the
    registry does not know is skipped rather than invented.
    """
    paragraphs = []
    block = document.begin()
    while block.isValid():
        indent = clamp_indent(block.blockFormat().indent())
        paragraphs.append(Paragraph(indent, coalesce_runs(_paragraph_runs(block, embeds))))
        block = block.next()
    return WriteDocument(paragraphs)


def _slice_utf16(text: str, start: int, end: int) -> str:
    """`text[start:end]` where the offsets count UTF-16 code units, as Qt positions do.

    A Python `str` indexes code points, Qt indexes UTF-16 units, so the two agree
    only while every character is in the Basic Multilingual Plane. Cutting in the
    middle of a surrogate pair leaves a lone surrogate, which `sanitize_text`
    turns into U+FFFD.
    """
    if text.isascii():
        return text[start:end]
    data = text.encode("utf-16-le", "surrogatepass")
    return data[2 * start : 2 * end].decode("utf-16-le", "surrogatepass")


def runs_in_range(document: QTextDocument, lo: int, hi: int) -> list:
    """Text runs covering document positions `[lo, hi)`, coalesced, image fragments skipped.

    Used to capture an Embed's `source`. Positions are absolute character
    positions (the same numbers `QTextCursor.selectionStart/End` return, which
    count UTF-16 units - a fragment is cut with `_slice_utf16`, so an emoji
    earlier in the fragment cannot shift the slice). A
    range that spans several blocks yields the runs of all of them
    concatenated with no separator - callers that need one paragraph (Embed,
    decision D4) refuse a multi-block range first.
    """
    if hi <= lo:
        return []
    runs: list = []
    block = document.findBlock(lo)
    while block.isValid() and block.position() < hi:
        for frag in block_fragments(block):
            start = max(lo, frag.position())
            end = min(hi, frag.position() + frag.length())
            if end <= start:
                continue
            fmt = frag.charFormat()
            if fmt.isImageFormat():
                continue
            text = sanitize_text(_slice_utf16(frag.text(), start - frag.position(), end - frag.position()))
            if text:
                runs.append(Run(text, tags_from_char_format(fmt)))
        block = block.next()
    return coalesce_runs(runs)


# -- PNG decode (checked) -------------------------------------------------------


def decode_embed_png(png: bytes) -> QImage:
    """Decode an embed PNG to a `QImage`, refusing anything suspicious first.

    Order matters: `check_png` (signature, IHDR, dimension and pixel caps - all
    from a few header bytes, Qt-free) BEFORE any image library sees the data;
    then a `QImageReader` over an in-memory buffer whose reported size must equal
    the header's; then `read()`. Raises `ValueError` for every failure (a null
    image included). Never calls `QImageReader.setAllocationLimit`, which is
    process-global.
    """
    width, height = check_png(png)
    buffer = QBuffer()
    buffer.setData(QByteArray(png))
    buffer.open(QIODevice.OpenModeFlag.ReadOnly)
    reader = QImageReader(buffer, QByteArray(b"png"))
    size = reader.size()
    if not size.isValid() or (size.width(), size.height()) != (width, height):
        raise ValueError("PNG size disagrees with its header")
    image = reader.read()
    if image.isNull():
        raise ValueError(f"PNG could not be decoded: {reader.errorString()}")
    return image


# -- model -> document ----------------------------------------------------------


def _detach_highlighters(document: QTextDocument, highlighter) -> list:
    found = []
    if highlighter is not None:
        found.append(highlighter)
    for child in document.findChildren(QSyntaxHighlighter):
        if child not in found:
            found.append(child)
    detached = [h for h in found if h.document() is document]
    for h in detached:
        h.setDocument(None)
    return detached


def _insert_source_text(cursor: QTextCursor, source, warnings: list, where: str, reason: str) -> None:
    for run in source:
        text = sanitize_text(run.text)
        if text:
            cursor.insertText(text, char_format_from_tags(run.tags))
    warnings.append(f"{where}: embed shown as its source text ({reason})")


def _insert_embed(cursor, document, run, embeds, decode, cache, warnings, where) -> None:
    try:
        image = cache.get(run.png)
        if image is None:
            image = decode(run.png)
            cache[run.png] = image
        embed_id = embeds.add(run.png, run.source)
    except ValueError as exc:  # bad/bomb PNG, or the registry is full
        _insert_source_text(cursor, run.source, warnings, where, str(exc))
        return
    name = f"{EMBED_URL_SCHEME}://{embed_id}"
    document.addResource(QTextDocument.ResourceType.ImageResource, QUrl(name), image)
    image_format = QTextImageFormat()
    image_format.setName(name)
    image_format.setWidth(image.width())
    image_format.setHeight(image.height())
    image_format.setProperty(PROP_EMBED, embed_id)
    cursor.insertImage(image_format)


def model_to_document(model: WriteDocument, document: QTextDocument, embeds, *, highlighter=None, decode=None) -> list:
    """Replace the contents of `document` with `model`; return a list of warning strings.

    Sequence (each step is load-bearing): undo disabled first so nothing is
    recorded; every attached highlighter detached (the explicit `highlighter`
    plus any parented to the document) so it neither runs per block during the
    build nor writes formats into a half-built document - it is re-attached at
    the end and does its one pass; `clear()`; ONE cursor inside ONE edit block;
    each block starts from `insertBlock(blockFormat, QTextCharFormat())` (a clean
    char format, so a bold run cannot bleed into the next paragraph); text runs
    go in with `char_format_from_tags` (a fresh format, never merged); embeds are
    decoded (`decode`, default `decode_embed_png`), added to `embeds`, registered
    as an `ImageResource` under `mem://<id>` and inserted, then the typing format
    is reset (V7). A decode failure or a full registry inserts the embed's source
    text and adds a warning. Finally `setIndentWidth(24)`, undo re-enabled and
    `setModified(False)`: a freshly loaded document has no history and is clean.
    Run text is re-sanitised so a hand-built model cannot break the
    one-paragraph-one-block rule. Note for callers: this emits `contentsChange`
    like any edit - an editor that treats that signal as "the user edited" must
    block it around a load.
    """
    warnings: list = []
    decode = decode or decode_embed_png
    cache: dict = {}
    document.setUndoRedoEnabled(False)
    detached = _detach_highlighters(document, highlighter)
    try:
        document.clear()
        cursor = QTextCursor(document)
        cursor.beginEditBlock()
        try:
            for number, paragraph in enumerate(model.paragraphs):
                where = f"paragraph {number + 1}"
                block_format = QTextBlockFormat()
                block_format.setIndent(clamp_indent(paragraph.indent))
                if number == 0:
                    cursor.setBlockFormat(block_format)  # the document's own first (empty) block
                else:
                    cursor.insertBlock(block_format, QTextCharFormat())
                for run in paragraph.runs:
                    if isinstance(run, EmbedRun):
                        _insert_embed(cursor, document, run, embeds, decode, cache, warnings, where)
                        cursor.setCharFormat(QTextCharFormat())
                        continue
                    text = sanitize_text(run.text)
                    if text != run.text:
                        warnings.append(f"{where}: control or newline characters were removed from a run")
                    if text:
                        cursor.insertText(text, char_format_from_tags(run.tags))
        finally:
            cursor.endEditBlock()
        document.setIndentWidth(INDENT_WIDTH_PX)
    finally:
        document.setUndoRedoEnabled(True)
        document.setModified(False)
        for h in detached:
            h.setDocument(document)
    return warnings
