"""Tests for write_docmodel.py - the model <-> QTextDocument bridge (hazard B).

Headless: a bare `QTextDocument` needs no widget, only the shared `qapp`. The
`write_qt` fixture is applied anyway (module-level `pytestmark`) so this file
follows the same Qt lifetime rules as every other Write Qt test file. Source
stays ASCII (special characters spelled as escapes or names, printed strings
through `ascii()`).
"""

import sys
from pathlib import Path

_MVP_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_MVP_ROOT / "SAP Modifier"))

import pytest
from PySide6.QtCore import QBuffer, QIODevice, Qt
from PySide6.QtGui import (
    QFont,
    QImage,
    QImageReader,
    QSyntaxHighlighter,
    QTextCharFormat,
    QTextCursor,
    QTextDocument,
    QTextFormat,
)

import write_testkit
from write_docmodel import (
    LEAN_LEFT,
    OBJECT_CHAR,
    PROP_DASH,
    PROP_EMBED,
    PROP_LEAN,
    apply_tags_to_format,
    block_fragments,
    char_format_from_tags,
    document_to_model,
    embed_id_of,
    is_dash,
    is_embed_format,
    is_left_lean,
    model_to_document,
    runs_in_range,
    set_tag,
    tags_from_char_format,
)
from write_model import EmbedRegistry, EmbedRun, Paragraph, Run, WriteDocument, normalize_tags
from write_testkit import write_qt  # noqa: F401  (fixture: see write_testkit's Qt lifetime rules)

pytestmark = pytest.mark.usefixtures("write_qt")


def _png(width=8, height=6, color=Qt.GlobalColor.red) -> bytes:
    image = QImage(width, height, QImage.Format.Format_ARGB32)
    image.fill(color)
    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    assert image.save(buffer, "PNG")
    return bytes(buffer.data())


def _image_fragments(document):
    found = []
    block = document.begin()
    while block.isValid():
        for frag in block_fragments(block):
            if frag.charFormat().isImageFormat():
                found.append(frag)
        block = block.next()
    return found


def _sample_model(png_a, png_b):
    source = [Run("Goals: x", {"bold": True, "spread": 2.0}), Run(" more", {"italic": "left"})]
    return WriteDocument(
        [
            Paragraph(
                0,
                [
                    Run("Phrase: ", {"bold": True}),
                    Run("plain ", {}),
                    Run("lean", {"italic": "left", "dash": True}),
                    Run(" tail", {"line": True, "spread": 2.5}),
                ],
            ),
            Paragraph(3, [Run("right", {"italic": "right", "bold": True})]),
            Paragraph(1, []),
            Paragraph(
                2,
                [Run("before ", {}), EmbedRun(png_a, source), Run(" after", {"dash": True}), EmbedRun(png_b, [Run("Info: y")])],
            ),
        ]
    )


def test_round_trip_all_tags_indent_and_embed(qapp):
    registry = EmbedRegistry()
    model = _sample_model(_png(8, 6), _png(5, 5, Qt.GlobalColor.blue))
    document = QTextDocument()

    warnings = model_to_document(model, document, registry)

    assert warnings == []
    assert document.blockCount() == 4
    assert [document.findBlockByNumber(n).blockFormat().indent() for n in range(4)] == [0, 3, 1, 2]
    assert len(registry) == 2
    assert OBJECT_CHAR in document.findBlockByNumber(3).text()
    assert len(_image_fragments(document)) == 2
    back = document_to_model(document, registry)
    assert back.paragraphs == model.paragraphs
    # embeds keep their picture bytes and the source text (with its tags) underneath
    embeds = [r for r in back.paragraphs[3].runs if isinstance(r, EmbedRun)]
    assert [e.png for e in embeds] == [r.png for r in model.paragraphs[3].runs if isinstance(r, EmbedRun)]
    assert embeds[0].source == [Run("Goals: x", {"bold": True, "spread": 2.0}), Run(" more", {"italic": "left"})]
    # running it again is stable (load -> read -> load -> read)
    document2 = QTextDocument()
    model_to_document(back, document2, registry)
    assert document_to_model(document2, registry).paragraphs == model.paragraphs
    # runs_in_range: sub-range of one paragraph, clipped mid-fragment, tags intact
    block = document.findBlockByNumber(0)
    lo = block.position() + len("Phrase: ") - 2
    hi = block.position() + len("Phrase: plain ") + 2
    assert runs_in_range(document, lo, hi) == [
        Run(": ", {"bold": True}),
        Run("plain ", {}),
        Run("le", {"italic": "left", "dash": True}),
    ]
    assert runs_in_range(document, 5, 5) == []


class _LoudHighlighter(QSyntaxHighlighter):
    """Sets every text property the model knows (and more), on every block."""

    def __init__(self, document):
        self.calls = 0
        super().__init__(document)

    def highlightBlock(self, text):
        self.calls += 1
        fmt = QTextCharFormat()
        fmt.setFontWeight(700)
        fmt.setFontItalic(True)
        fmt.setFontStrikeOut(True)
        fmt.setFontLetterSpacingType(QFont.SpacingType.AbsoluteSpacing)
        fmt.setFontLetterSpacing(6.0)
        fmt.setProperty(PROP_DASH, True)
        fmt.setProperty(PROP_LEAN, LEAN_LEFT)
        fmt.setForeground(Qt.GlobalColor.red)
        self.setFormat(0, len(text), fmt)


def test_highlighter_formats_never_leak_into_tags(qapp):
    registry = EmbedRegistry()
    model = WriteDocument(
        [
            Paragraph(0, [Run("Phrase: plain words", {})]),
            Paragraph(1, [Run("only ", {}), Run("bold", {"bold": True})]),
            Paragraph(0, []),
        ]
    )
    document = QTextDocument()
    highlighter = _LoudHighlighter(document)

    model_to_document(model, document, registry, highlighter=highlighter)
    # detached during the build (no per-block passes into a half-built document)...
    assert highlighter.calls == 0
    # ...and re-attached afterwards
    assert highlighter.document() is document
    highlighter.rehighlight()
    assert highlighter.calls > 0

    # the highlighter really did write loud formats into the layout overlay...
    formats = document.findBlockByNumber(0).layout().formats()
    assert formats and formats[0].format.fontWeight() == 700 and formats[0].format.fontItalic()
    # ...but none of it is visible to the model
    back = document_to_model(document, registry)
    assert back.paragraphs == model.paragraphs
    assert all(run.tags == {} for run in back.paragraphs[0].runs)
    assert back.paragraphs[1].runs[1].tags == {"bold": True}
    # and the document's own fragment formats are untouched
    fragment = next(block_fragments(document.findBlockByNumber(0)))
    assert fragment.charFormat().fontWeight() == 400
    assert not fragment.charFormat().fontItalic()
    assert not fragment.charFormat().boolProperty(PROP_DASH)


@pytest.mark.parametrize(
    "tags",
    [
        {},
        {"bold": True},
        {"line": True},
        {"dash": True},
        {"italic": "right"},
        {"italic": "left"},
        {"spread": 3.0},
        {"spread": -2.0},
        {"bold": True, "line": True, "dash": True, "italic": "left", "spread": 1.5},
    ],
)
def test_char_format_and_tags_map_each_tag(qapp, tags):
    fmt = char_format_from_tags(tags)
    assert tags_from_char_format(fmt) == normalize_tags(tags)
    assert (fmt.fontWeight() == 700) == bool(tags.get("bold"))
    assert fmt.fontStrikeOut() == bool(tags.get("line"))
    assert fmt.boolProperty(PROP_DASH) == bool(tags.get("dash")) == is_dash(fmt)
    assert (fmt.intProperty(PROP_LEAN) == LEAN_LEFT) == (tags.get("italic") == "left") == is_left_lean(fmt)
    assert fmt.fontItalic() == (tags.get("italic") == "right")
    if "spread" in tags:
        assert fmt.hasProperty(QTextFormat.Property.FontLetterSpacing)
        assert fmt.fontLetterSpacingType() == QFont.SpacingType.AbsoluteSpacing
        assert fmt.fontLetterSpacing() == tags["spread"]
    else:
        assert not fmt.hasProperty(QTextFormat.Property.FontLetterSpacing)

    # off-values overwrite (merging cannot delete a property) and read back as absent
    for key in ("bold", "line", "dash", "italic", "spread"):
        again = char_format_from_tags(tags)
        set_tag(again, key, None)
        expected = {k: v for k, v in normalize_tags(tags).items() if k != key}
        assert tags_from_char_format(again) == expected, key
    cleared = apply_tags_to_format(char_format_from_tags(tags), {})
    assert tags_from_char_format(cleared) == {}
    assert cleared.fontWeight() == 400 and cleared.intProperty(PROP_LEAN) == 0 and not cleared.boolProperty(PROP_DASH)
    assert cleared.hasProperty(PROP_LEAN) and cleared.hasProperty(PROP_DASH)  # explicit off-values, not removals


def test_format_edge_cases_and_lookalikes(qapp):
    # left lean wins over a stray native italic flag
    both = QTextCharFormat()
    both.setFontItalic(True)
    both.setProperty(PROP_LEAN, LEAN_LEFT)
    assert tags_from_char_format(both) == {"italic": "left"}
    # weight 599 is not bold, 600 is (the read threshold)
    for weight, bold in ((599, False), (600, True), (400, False), (900, True)):
        fmt = QTextCharFormat()
        fmt.setFontWeight(weight)
        assert tags_from_char_format(fmt).get("bold", False) is bold, weight
    # percentage letter-spacing (not ours) and zero absolute spacing are not a spread tag
    pct = QTextCharFormat()
    pct.setFontLetterSpacingType(QFont.SpacingType.PercentageSpacing)
    pct.setFontLetterSpacing(150.0)
    assert tags_from_char_format(pct) == {}
    zero = QTextCharFormat()
    zero.setFontLetterSpacingType(QFont.SpacingType.AbsoluteSpacing)
    zero.setFontLetterSpacing(0.0)
    assert tags_from_char_format(zero) == {}
    # out-of-range spread in a foreign format is clamped by the canonical form
    wide = QTextCharFormat()
    wide.setFontLetterSpacingType(QFont.SpacingType.AbsoluteSpacing)
    wide.setFontLetterSpacing(500.0)
    assert tags_from_char_format(wide) == {"spread": 32.0}
    # an image format has no text tags, is never dash/lean, and is an embed only with the property
    document = QTextDocument()
    registry = EmbedRegistry()
    model_to_document(WriteDocument([Paragraph(0, [EmbedRun(_png(), [Run("x")])])]), document, registry)
    image_fmt = _image_fragments(document)[0].charFormat()
    assert is_embed_format(image_fmt) and embed_id_of(image_fmt) in registry
    assert tags_from_char_format(image_fmt) == {} and not is_dash(image_fmt) and not is_left_lean(image_fmt)
    image_fmt.setProperty(PROP_DASH, True)
    assert not is_dash(image_fmt)
    plain_image = QTextCharFormat()
    assert not is_embed_format(plain_image) and embed_id_of(plain_image) is None
    # a spread tag survives a real insert/read cycle at 2 dp
    document2 = QTextDocument()
    model_to_document(WriteDocument([Paragraph(0, [Run("ab", {"spread": 2.126})])]), document2, registry)
    assert document_to_model(document2, registry).paragraphs[0].runs == [Run("ab", {"spread": 2.13})]


def test_adjacent_identical_embeds_merge_into_one_fragment_but_read_back_separately(qapp):
    registry = EmbedRegistry()
    png_a, png_b = _png(8, 6), _png(9, 7, Qt.GlobalColor.green)
    source_a, source_b = [Run("Goals: a")], [Run("Goals: b")]
    model = WriteDocument(
        [
            Paragraph(
                0,
                [
                    EmbedRun(png_a, source_a),
                    EmbedRun(png_a, source_a),
                    EmbedRun(png_a, source_a),
                    EmbedRun(png_b, source_b),
                ],
            )
        ]
    )
    document = QTextDocument()
    assert model_to_document(model, document, registry) == []

    # V7: the three identical images are ONE fragment (OBJECT_CHAR x 3); the different one is its own
    fragments = _image_fragments(document)
    assert [frag.text() for frag in fragments] == [OBJECT_CHAR * 3, OBJECT_CHAR]
    assert len(registry) == 2  # deduplicated by content

    back = document_to_model(document, registry)
    assert len(back.paragraphs) == 1
    runs = back.paragraphs[0].runs
    assert runs == model.paragraphs[0].runs
    assert len(runs) == 4 and all(isinstance(run, EmbedRun) for run in runs)
    assert runs[0] is not runs[1]  # independent copies, safe to mutate separately
    runs[0].source.append(Run("mutated"))
    assert document_to_model(document, registry).paragraphs[0].runs[1].source == source_a

    # an image the registry never heard of is skipped, not invented
    other = EmbedRegistry()
    assert document_to_model(document, other).paragraphs[0].runs == []


def test_text_after_an_image_has_no_embed_property(qapp):
    registry = EmbedRegistry()
    model = WriteDocument(
        [Paragraph(0, [EmbedRun(_png(), [Run("Goals: g")]), Run("after", {"bold": True}), Run(" more", {})])]
    )
    document = QTextDocument()
    model_to_document(model, document, registry)

    block = document.findBlockByNumber(0)
    kinds = [(frag.charFormat().isImageFormat(), frag.charFormat().hasProperty(PROP_EMBED), frag.text()) for frag in block_fragments(block)]
    assert kinds == [(True, True, OBJECT_CHAR), (False, False, "after"), (False, False, " more")]

    # V7's trap: typed text right after insertImage inherits the image format's property.
    # Simulate exactly that fragment; the reader must call it text (isImageFormat + property together).
    leaked = QTextCharFormat()
    leaked.setProperty(PROP_EMBED, embed_id_of(next(iter(_image_fragments(document))).charFormat()))
    cursor = QTextCursor(document)
    cursor.movePosition(QTextCursor.MoveOperation.End)
    cursor.insertText(" typed", leaked)
    back = document_to_model(document, registry)
    last = back.paragraphs[0].runs[-1]
    assert isinstance(last, Run) and last.text == " more typed" and last.tags == {}  # merged with its text neighbour
    assert sum(isinstance(run, EmbedRun) for run in back.paragraphs[0].runs) == 1
    assert not is_embed_format(leaked) and tags_from_char_format(leaked) == {}


def test_load_clears_undo_and_modified_and_sets_indent_width(qapp):
    registry = EmbedRegistry()
    document = QTextDocument()
    cursor = QTextCursor(document)
    cursor.insertText("old content that had history")
    assert document.isUndoAvailable() and document.isModified()

    model = WriteDocument([Paragraph(2, [Run("Info: new")]), Paragraph(0, [Run("second", {"bold": True})])])
    model_to_document(model, document, registry)

    assert document.toPlainText() == "Info: new\nsecond"
    assert not document.isUndoAvailable() and not document.isRedoAvailable()
    assert not document.isModified()
    assert document.isUndoRedoEnabled()  # re-enabled: later edits are undoable
    assert document.indentWidth() == 24
    assert document.findBlockByNumber(0).blockFormat().indent() == 2

    # a later edit is one undoable step that does not reach back into the load
    edit = QTextCursor(document)
    edit.movePosition(QTextCursor.MoveOperation.End)
    edit.insertText("!")
    assert document.isModified() and document.isUndoAvailable()
    document.undo()
    assert document.toPlainText() == "Info: new\nsecond"
    assert not document.isUndoAvailable()

    # loading an empty model still leaves one clean empty block
    model_to_document(WriteDocument([]), document, registry)
    assert document.blockCount() == 1 and document.toPlainText() == ""
    assert document_to_model(document, registry).paragraphs == [Paragraph(0, [])]


def test_undecodable_embed_degrades_to_source_text_with_a_warning(qapp):
    limit_before = QImageReader.allocationLimit()
    source = [Run("Goals: keep ", {"bold": True}), Run("me", {"italic": "left"})]
    good = _png()
    bomb = write_testkit.fake_png_header(5000, 5000)  # header alone is enough to refuse it
    headless = write_testkit.fake_png_header(20, 20)  # passes the header check, cannot decode
    garbage = b"definitely not a png"
    model = WriteDocument(
        [
            Paragraph(0, [EmbedRun(good, [Run("ok")])]),
            Paragraph(1, [Run("a ", {}), EmbedRun(bomb, source), Run(" z", {})]),
            Paragraph(2, [EmbedRun(headless, source)]),
            Paragraph(3, [EmbedRun(garbage, source)]),
        ]
    )
    registry = EmbedRegistry()
    document = QTextDocument()

    warnings = model_to_document(model, document, registry)

    assert len(warnings) == 3
    assert all("source text" in w for w in warnings)
    assert warnings[0].startswith("paragraph 2") and warnings[2].startswith("paragraph 4")
    assert len(_image_fragments(document)) == 1  # only the good one became a picture
    assert len(registry) == 1  # a refused picture is never stored
    back = document_to_model(document, registry)
    assert back.paragraphs[1].runs == [Run("a ", {}), Run("Goals: keep ", {"bold": True}), Run("me", {"italic": "left"}), Run(" z", {})]
    assert back.paragraphs[2].runs == source
    assert back.paragraphs[3].runs == source
    assert isinstance(back.paragraphs[0].runs[0], EmbedRun)
    # the process-global allocation limit is never touched
    assert QImageReader.allocationLimit() == limit_before

    # a full registry degrades the same way (the picture is fine, the budget is not)
    tiny = EmbedRegistry(max_embeds=0)
    document2 = QTextDocument()
    warnings2 = model_to_document(WriteDocument([Paragraph(0, [EmbedRun(good, source)])]), document2, tiny)
    assert len(warnings2) == 1 and "too many embeds" in warnings2[0]
    assert document_to_model(document2, tiny).paragraphs[0].runs == source

    # the decoder is injectable, and a failure from it degrades too
    calls = []

    def refusing(png):
        calls.append(len(png))
        raise ValueError("no thanks")

    document3 = QTextDocument()
    warnings3 = model_to_document(WriteDocument([Paragraph(0, [EmbedRun(good, source)])]), document3, EmbedRegistry(), decode=refusing)
    assert calls == [len(good)] and "no thanks" in warnings3[0]
