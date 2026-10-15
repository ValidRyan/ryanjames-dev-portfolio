"""Tests for write_editor.py - the editor widget: paste guard, seven tools, undo, keys, completer.

Every test builds a real `WriteEditor` (never a stand-in): the guards live in its
overrides, so only the real class can prove them. The paste / drop tests feed it
hostile MIME data (HTML with `file://` and UNC `<img>` sources, URL lists) and count the
`loadResource` requests Qt makes through a spy subclass - the spy RECORDS and never
delegates, so nothing here can touch the network or a file share. A control test proves
the hazard is real (a plain `QTextEdit` with the default paste DOES ask for the pictures),
otherwise "zero requests" would prove nothing.

Tool tests read the result through `editor.to_model()` (document formats only, off-values
read back as absent tags). The undo test is parametrised over every tool: each one must be
exactly one `document().undo()`. Pixel tests follow write_testkit's rules: the offscreen
platform has NO fonts (glyphs are boxes), so text is painted RED through the document's own
format while the overlay draws in the palette Text colour BLACK, and assertions are ratios
and ranges measured inside one image.

Every test runs under the `write_qt` fixture and registers the widgets it builds with
`write_testkit.track_qt`. Source stays ASCII: the object-replacement character, line
separators and non-BMP characters are built with `chr()`, and printed strings go through
`ascii()`.

Native-crash trap found while writing these: `QTest.keyClick(widget, "\\n")` and
`QTest.keyClicks(widget, "...\\n...")` (a NEWLINE in the string) kill the whole process
with a silent exit code 127 - even on a stock `QTextEdit` with no Write code involved. Press
Enter with `QTest.keyClick(widget, Qt.Key.Key_Return)` and never put a newline in a
`keyClicks` string.
"""

import ast
import sys
from pathlib import Path

_MVP_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_MVP_ROOT / "SAP Modifier"))

import pytest
from PySide6.QtCore import QBuffer, QIODevice, QMimeData, QPoint, QPointF, Qt, QUrl
from PySide6.QtGui import (
    QColor,
    QDragEnterEvent,
    QDragMoveEvent,
    QDropEvent,
    QFont,
    QImage,
    QPalette,
    QTextCharFormat,
    QTextCursor,
    QTextDocument,
    QTextFormat,
)
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QTextEdit

import write_editor
import write_grammar
import write_paths
import write_testkit
from write_docmodel import LEAN_LEFT, PROP_DASH, PROP_EMBED, PROP_LEAN, block_fragments, is_dash, is_left_lean
from write_editor import MAX_PASTE_CHARS, WriteEditor, sanitize_paste
from write_grammar import INSTRUCTION_NAMES
from write_highlight import WriteHighlighter
from write_model import MAX_INDENT, SPREAD_MAX, SPREAD_MIN, SPREAD_STEP, EmbedRun, Paragraph, Run, WriteDocument
from write_testkit import write_qt  # noqa: F401  (fixture: see write_testkit's Qt lifetime rules)

pytestmark = pytest.mark.usefixtures("write_qt")

OBJECT_CHAR = chr(0xFFFC)
LINE_SEP = chr(0x2028)
PARA_SEP = chr(0x2029)
EMOJI = chr(0x1F600)  # one code point, two UTF-16 units
WHITE = QColor(255, 255, 255)
BLACK = QColor(0, 0, 0)
RED = QColor(255, 0, 0)
BLUE = QColor(0, 0, 200)

_IMAGE = QTextDocument.ResourceType.ImageResource.value
_P = QTextFormat.Property


# -- helpers ----------------------------------------------------------------------------------


def _png(width=8, height=6, color=QColor(200, 30, 30)) -> bytes:
    image = QImage(width, height, QImage.Format.Format_ARGB32)
    image.fill(color)
    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    assert image.save(buffer, "PNG")
    return bytes(buffer.data())


def _palette() -> QPalette:
    pal = QPalette()
    for role, color in (
        (QPalette.ColorRole.Base, WHITE),
        (QPalette.ColorRole.Window, WHITE),
        (QPalette.ColorRole.Text, BLACK),
        (QPalette.ColorRole.WindowText, BLACK),
        (QPalette.ColorRole.Highlight, BLUE),
        (QPalette.ColorRole.HighlightedText, WHITE),
    ):
        pal.setColor(role, color)
    return pal


class _Probe:
    """Plain-Python receiver for the editor's signals (bound methods, no closure over Qt state)."""

    def __init__(self):
        self.edits = 0
        self.states = []
        self.statuses = []
        self.errors = []
        self.text_changed = 0
        self.contents_changed = 0

    def on_edited(self):
        self.edits += 1

    def on_state(self, state):
        self.states.append(dict(state))

    def on_status(self, message):
        self.statuses.append(message)

    def on_error(self, message):
        self.errors.append(message)

    def on_text_changed(self):
        self.text_changed += 1

    def on_contents_changed(self):
        self.contents_changed += 1


class _SpyEditor(WriteEditor):
    """A WriteEditor that records every `loadResource` request (the SMB-leak canary); it never delegates to Qt's loader."""

    def __init__(self, parent=None, root_file=None):
        self.resource_calls = []
        super().__init__(parent, root_file)

    def loadResource(self, type_, name):
        self.resource_calls.append((int(type_), name.toString()))
        return super().loadResource(type_, name)


class _ControlEdit(QTextEdit):
    """A stock `QTextEdit` (default paste) that records `loadResource` requests and answers None: no I/O possible."""

    def __init__(self):
        self.resource_calls = []
        super().__init__()

    def loadResource(self, type_, name):
        self.resource_calls.append(name.toString())
        return None


class _FocusedEditor(WriteEditor):
    """A WriteEditor that claims focus on demand (`hasFocus` is not virtual: only the Python-side caret check sees it)."""

    def __init__(self, parent=None, root_file=None):
        self.focused = False
        super().__init__(parent, root_file)

    def hasFocus(self):
        return self.focused


@pytest.fixture
def clean_clipboard(qapp):
    """Empty the clipboard after a test that put a Python-made `QMimeData` on it.

    A Python-made payload still on the clipboard when the interpreter exits kills the whole
    pytest process (exit 139) - see the module docstring of write_editor.py. Clearing while
    the interpreter is alive deletes it safely.
    """
    yield
    QApplication.clipboard().clear()


def _make_editor(model=None, *, cls=WriteEditor, root_file=None, size=(480, 160), point_size=None, palette=None):
    """A tracked `WriteEditor` (never shown unless a test shows it) with a signal probe; returns `(editor, probe)`."""
    editor = write_testkit.track_qt(cls(root_file=root_file))
    editor.resize(*size)
    if palette is not None:
        editor.setPalette(palette)
    if point_size:
        font = QFont()
        font.setPointSize(point_size)
        editor.setFont(font)
    probe = _Probe()
    editor.content_edited.connect(probe.on_edited)
    editor.tool_state_changed.connect(probe.on_state)
    editor.status_message.connect(probe.on_status)
    editor.error_message.connect(probe.on_error)
    if model is not None:
        assert editor.load_model(model) == []
    return editor, probe


def _doc(*paragraphs) -> WriteDocument:
    """`paragraphs` are `(indent, [runs])` pairs."""
    return WriteDocument([Paragraph(indent, list(runs)) for indent, runs in paragraphs])


def _select(editor, lo, hi=None):
    cursor = editor.textCursor()
    cursor.setPosition(lo)
    if hi is not None:
        cursor.setPosition(hi, QTextCursor.MoveMode.KeepAnchor)
    editor.setTextCursor(cursor)


def _end(editor) -> int:
    return editor.document().characterCount() - 1


def _select_all(editor):
    _select(editor, 0, _end(editor))


def _starts(editor) -> list:
    document = editor.document()
    return [document.findBlockByNumber(n).position() for n in range(document.blockCount())]


def _paragraph(editor, number=0):
    return editor.to_model().paragraphs[number]


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
            rgb = image.pixel(x, y)
            yield x, y, (rgb >> 16) & 255, (rgb >> 8) & 255, rgb & 255


def _ink_points(image: QImage, predicate) -> list:
    return [(x, y) for x, y, r, g, b in _pixels(image) if predicate(r, g, b)]


def _dark(r, g, b) -> bool:  # overlay ink: the palette Text colour BLACK
    return r < 90 and g < 90 and b < 90


def _reddish(r, g, b) -> bool:  # base ink: the document's own foreground RED
    return r > 170 and g < 90 and b < 90


def _paint_text_red(editor):
    """Give every character a RED foreground through the document's own formats (not a tag)."""
    cursor = QTextCursor(editor.document())
    cursor.select(QTextCursor.SelectionType.Document)
    fmt = QTextCharFormat()
    fmt.setForeground(RED)
    cursor.mergeCharFormat(fmt)


def _grab(editor) -> QImage:
    """Render the editor (forces a real layout and a real paintEvent)."""
    return editor.grab().toImage().convertToFormat(QImage.Format.Format_ARGB32)


def _source_tree() -> ast.Module:
    return ast.parse((_MVP_ROOT / "SAP Modifier" / "write_editor.py").read_text(encoding="utf-8"))


# -- 1. configuration -----------------------------------------------------------------------------


def test_configuration(qapp):
    editor, _ = _make_editor()
    document = editor.document()
    assert editor.acceptRichText() is False
    assert editor.autoFormatting() == QTextEdit.AutoFormattingFlag.AutoNone
    assert document.indentWidth() == 24
    assert editor.styleSheet() == "", "a stylesheet would change what the overlay covers with"
    assert editor.tabChangesFocus() is False
    assert not document.isModified() and not document.isUndoAvailable()
    # a highlighter is attached, the keyword completer is wired to the eight names in octagon order
    assert isinstance(editor.highlighter, WriteHighlighter) and editor.highlighter.document() is document
    completer = editor.completer
    model = completer.model()
    assert [model.index(row, 0).data() for row in range(model.rowCount())] == list(INSTRUCTION_NAMES)
    assert completer.caseSensitivity() == Qt.CaseSensitivity.CaseInsensitive
    assert completer.widget() is editor
    assert len(editor.embeds) == 0 and editor.root_file is None
    # the four signals exist with the documented shape
    for name in ("content_edited", "tool_state_changed", "status_message", "error_message"):
        assert hasattr(editor, name), name
    # the source never reaches for HTML, the base-class resource loader or the (process-global) allocation limit
    names = set()
    for node in ast.walk(_source_tree()):
        if isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, ast.Name):
            names.add(node.id)
    assert names.isdisjoint({"setHtml", "insertHtml", "toHtml", "html", "hasHtml", "setMarkdown", "setAllocationLimit"})
    load_resource = [n for n in ast.walk(_source_tree()) if isinstance(n, ast.FunctionDef) and n.name == "loadResource"]
    assert len(load_resource) == 1
    assert not [n for n in ast.walk(load_resource[0]) if isinstance(n, ast.Attribute) and n.attr == "loadResource"]


# -- 2. hostile HTML paste ----------------------------------------------------------------------------


HOSTILE_HTML = (
    '<html><body><p>Look: <img src="file://attacker-host.invalid/share/pixel.png">'
    '<img src="\\\\attacker-host.invalid\\share\\evil.png" width="9" height="9">'
    '<b style="font-size:40pt">bold</b></p></body></html>'
)


def _hostile_mime(text="hello world") -> QMimeData:
    mime = QMimeData()
    mime.setHtml(HOSTILE_HTML)
    if text is not None:
        mime.setText(text)
    return mime


@pytest.mark.usefixtures("clean_clipboard")
def test_hostile_html_paste_inserts_plain_text_only_and_makes_zero_resource_calls(qapp):
    # control: a stock QTextEdit with the default paste DOES ask for the pictures (so "zero" below means something)
    control = write_testkit.track_qt(_ControlEdit())
    control.resize(300, 200)
    control.insertFromMimeData(_hostile_mime())
    control.show()  # the pictures are requested when the document is laid out
    QTest.qWait(30)
    assert control.resource_calls, "the hazard is real: the default paste requests the remote pictures"

    editor, probe = _make_editor(cls=_SpyEditor)
    mime = _hostile_mime("hello <b>world</b>")
    assert editor.canInsertFromMimeData(mime)
    editor.insertFromMimeData(mime)
    assert editor.toPlainText() == "hello <b>world</b>"  # the text/plain payload, verbatim; the HTML was never read
    assert _image_fragments(editor.document()) == []
    assert editor.to_model().paragraphs[0].runs == [Run("hello <b>world</b>")]  # no bold tag from the <b>

    # the real path: clipboard + paste()
    QApplication.clipboard().setMimeData(_hostile_mime("second"))
    editor.paste()
    assert editor.toPlainText() == "hello <b>world</b>second"
    assert _image_fragments(editor.document()) == []

    # an HTML-only payload carries no text: refused, and nothing is inserted even if forced
    html_only = QMimeData()
    html_only.setHtml(HOSTILE_HTML)
    assert not editor.canInsertFromMimeData(html_only)
    before = editor.toPlainText()
    editor.insertFromMimeData(html_only)
    assert editor.toPlainText() == before
    QApplication.clipboard().setMimeData(QMimeData())
    editor.paste()  # an empty clipboard is a no-op, not an error
    assert editor.toPlainText() == before

    # Qt had every chance: lay the document out, spin the loop; still nothing asked for a picture
    editor.show()
    QTest.qWait(30)
    assert editor.resource_calls == []
    assert probe.errors == []


# -- 3. a UNC <img> never becomes an image fragment ------------------------------------------------------------


def test_unc_img_never_becomes_an_image_fragment(qapp):
    editor, _ = _make_editor(cls=_SpyEditor)
    unc = "\\\\attacker-host.invalid\\share\\pic.png"
    html_variants = (
        '<img src="' + unc + '" width="50" height="50">',
        '<p><img src="file://attacker-host.invalid/share/pic.png"></p>',
        '<img src="file:///' + unc.replace("\\", "/") + '">',
        '<img src="//attacker-host.invalid/share/pic.png">',
        '<img src="data:image/png;base64,iVBORw0KGgo=">',
        '<a href="' + unc + '">link</a>',
    )
    for index, html in enumerate(html_variants):
        for carrier in ("html+text", "html+urls", "html+text+urls"):
            mime = QMimeData()
            mime.setHtml(html)
            if "text" in carrier:
                mime.setText(f"t{index}")
            if "urls" in carrier:
                mime.setUrls([QUrl("file://attacker-host.invalid/share/pic.png"), QUrl.fromLocalFile(unc)])
            editor.insertFromMimeData(mime)
            assert _image_fragments(editor.document()) == [], (index, carrier)
            assert OBJECT_CHAR not in editor.toPlainText(), (index, carrier)
    assert len(editor.embeds) == 0
    editor.show()
    QTest.qWait(30)
    assert editor.resource_calls == []
    # the model that would be saved holds text only
    assert all(isinstance(run, Run) for p in editor.to_model().paragraphs for run in p.runs)


# -- 4. paste sanitises and caps -------------------------------------------------------------------------------------


def test_paste_sanitises_and_caps(qapp, monkeypatch):
    raw = (
        "a" + LINE_SEP + "b" + PARA_SEP + "c" + "\v" + "d" + "\f" + "e" + OBJECT_CHAR + "f" + "\x01\x07" + "g\r\nh\ri\tj"
    )
    assert sanitize_paste(raw) == "a\nb\nc\nd\nef" + "g\nh\ni\tj"
    assert sanitize_paste("") == ""
    assert sanitize_paste("x" * (MAX_PASTE_CHARS + 50)) == "x" * MAX_PASTE_CHARS

    editor, probe = _make_editor()
    mime = QMimeData()
    mime.setText(raw)
    editor.insertFromMimeData(mime)
    assert editor.toPlainText() == "a\nb\nc\nd\nef" + "g\nh\ni\tj"
    assert editor.document().blockCount() == 7  # every soft or odd break became a real paragraph break
    assert LINE_SEP not in editor.toPlainText() and OBJECT_CHAR not in editor.toPlainText()

    # a multi-line paste keeps its lines (sanitising must not collapse them into spaces)
    second, _ = _make_editor()
    plain = QMimeData()
    plain.setText("line one\nline two")
    second.insertFromMimeData(plain)
    assert [p.runs[0].text for p in second.to_model().paragraphs] == ["line one", "line two"]
    assert second.document().blockCount() == 2

    # paste is one undo step, and replaces a selection
    third, _ = _make_editor(_doc((0, [Run("keep DELETE keep")])))
    _select(third, 5, 11)
    third.insertFromMimeData(plain)
    assert third.toPlainText() == "keep line one\nline two keep"
    third.document().undo()
    assert third.toPlainText() == "keep DELETE keep"

    # the length cap: cut, and said so
    big, big_probe = _make_editor()
    huge = QMimeData()
    huge.setText("x" * (MAX_PASTE_CHARS + 500))
    big.insertFromMimeData(huge)
    assert len(big.toPlainText()) == MAX_PASTE_CHARS
    assert any(str(MAX_PASTE_CHARS) in message for message in big_probe.statuses), big_probe.statuses
    at_limit = QMimeData()
    at_limit.setText("y" * 10)
    big_probe.statuses.clear()
    small, small_probe = _make_editor()
    small.insertFromMimeData(at_limit)
    assert small_probe.statuses == []  # nothing was cut, nothing is said

    # the paragraph budget (a document over the cap could not be saved): cut at the limit, and said so
    monkeypatch.setattr(write_editor, "MAX_PARAGRAPHS", 5)
    many, many_probe = _make_editor()
    lines = QMimeData()
    lines.setText("a\n" * 20)
    many.insertFromMimeData(lines)
    assert many.document().blockCount() == 5
    assert many.toPlainText() == "\n".join(["a"] * 5)
    assert any("paragraph limit" in message for message in many_probe.statuses), many_probe.statuses
    many.insertFromMimeData(lines)  # already at the limit: only the text of the first line goes in
    assert many.document().blockCount() == 5


# -- 5. drop ------------------------------------------------------------------------------------------------------------


def test_drop_of_urls_and_html_inserts_text_only(qapp):
    editor, probe = _make_editor(cls=_SpyEditor)
    editor.show()
    mime = QMimeData()
    mime.setUrls([QUrl("file://attacker-host.invalid/share/x.png"), QUrl("http://attacker-host.invalid/y.png")])
    mime.setHtml(HOSTILE_HTML)
    expected = sanitize_paste(mime.text())
    assert "attacker-host.invalid" in expected  # a URL-only drop hands over the URL strings as text (fact V10)

    point = QPoint(10, 10)
    enter = QDragEnterEvent(point, Qt.DropAction.CopyAction, mime, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
    assert QApplication.sendEvent(editor.viewport(), enter)
    move = QDragMoveEvent(point, Qt.DropAction.CopyAction, mime, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
    assert QApplication.sendEvent(editor.viewport(), move)
    drop = QDropEvent(QPointF(point), Qt.DropAction.CopyAction, mime, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
    QApplication.sendEvent(editor.viewport(), drop)

    assert editor.toPlainText() == expected
    assert _image_fragments(editor.document()) == [] and OBJECT_CHAR not in editor.toPlainText()
    assert editor.to_model().paragraphs[0].runs[0].tags == {}  # the html never made it into formatting
    QTest.qWait(30)
    assert editor.resource_calls == []
    assert probe.edits >= 1

    # a drop with no text at all (a picture / html only) is refused up front
    picture = QMimeData()
    picture.setImageData(QImage(4, 4, QImage.Format.Format_ARGB32))
    assert not editor.canInsertFromMimeData(picture)
    assert not editor.canInsertFromMimeData(None)


# -- 6. loadResource ---------------------------------------------------------------------------------------------------------


def test_load_resource_serves_only_registered_mem_pictures(qapp):
    editor, _ = _make_editor(cls=_SpyEditor)
    embed_id = editor.embeds.add(_png(9, 5), [Run("Goals: x")])
    served = editor.loadResource(_IMAGE, QUrl(f"mem://{embed_id}"))
    assert isinstance(served, QImage) and (served.width(), served.height()) == (9, 5)

    bad_id = editor.embeds.add(write_testkit.fake_png_header(20, 20), [Run("x")])  # header-valid, no pixel data
    others = (
        "http://attacker-host.invalid/x.png",
        "https://attacker-host.invalid/y.png",
        "file:///C:/Windows/win.ini",
        "file://attacker-host.invalid/share/x.png",
        "\\\\attacker-host.invalid\\share\\x.png",
        "qrc:/x.png",
        "ftp://attacker-host.invalid/x.png",
        "x.png",
        "",
        "mem://" + "0" * 40,  # the right scheme, an id the registry does not hold
        f"mem://{bad_id}",  # a held id whose picture is corrupt: refused by the checked decode
        f"mem2://{embed_id}",
    )
    for other in others:
        assert editor.loadResource(_IMAGE, QUrl(other)) is None, other
    # the right URL for the wrong kind of resource
    for kind in (QTextDocument.ResourceType.HtmlResource, QTextDocument.ResourceType.StyleSheetResource, QTextDocument.ResourceType.UnknownResource):
        assert editor.loadResource(kind.value, QUrl(f"mem://{embed_id}")) is None, kind
    # the spy has seen only the direct calls above (1 served + the refused URLs + the 3 kinds); Qt itself asked for nothing more
    direct_calls = 1 + len(others) + 3
    assert len(editor.resource_calls) == direct_calls
    editor.show()
    QTest.qWait(30)
    assert len(editor.resource_calls) == direct_calls


# -- 7. copy = source text -----------------------------------------------------------------------------------------------------------


@pytest.mark.usefixtures("clean_clipboard")
def test_copy_and_cut_put_plain_source_text_on_the_clipboard(qapp, monkeypatch):
    png = _png()
    source = [Run("Goals: "), Run("keep", {"bold": True})]
    model = _doc(
        (0, [Run("Phrase: ", {"bold": True}), Run("tilt", {"italic": "left", "dash": True})]),
        (2, [Run("a "), EmbedRun(png, source), Run(" b")]),
        (0, [EmbedRun(png, source), EmbedRun(png, source)]),  # identical neighbours merge into ONE image fragment (V7)
        (0, []),
        (0, [Run("l" + EMOJI + "ast", {"spread": 2.0})]),
    )
    editor, _ = _make_editor(model)
    starts = _starts(editor)
    assert starts == [0, 13, 19, 22, 23]
    assert len(_image_fragments(editor.document())) == 2  # the merged pair is one fragment

    def copied(lo, hi):
        _select(editor, lo, hi)
        mime = editor.createMimeDataFromSelection()
        assert mime.formats() == ["text/plain"] and not mime.hasHtml()
        return mime.text()

    whole = copied(0, _end(editor))
    assert ascii(whole) == ascii("Phrase: tilt\na Goals: keep b\nGoals: keepGoals: keep\n\nl" + EMOJI + "ast")
    assert OBJECT_CHAR not in whole
    assert copied(5, 17) == "e: tilt\na Goals: keep "  # ends between the picture and the rest of the line
    assert copied(15, 16) == "Goals: keep"  # just the picture
    assert copied(20, 21) == "Goals: keep"  # the second of two merged pictures
    assert copied(19, 21) == "Goals: keepGoals: keep"
    assert copied(0, 13) == "Phrase: tilt\n"  # ending exactly at a block start includes the break
    assert ascii(copied(starts[4] + 1, _end(editor))) == ascii(EMOJI + "ast")  # UTF-16 positions past a non-BMP character

    # no selection: an empty payload, nothing to copy
    _select(editor, 3)
    assert editor.createMimeDataFromSelection().formats() == []

    # the real clipboard path: copy() puts plain text (never html / rich formats) on it
    _select(editor, 0, _end(editor))
    editor.copy()
    clip = QApplication.clipboard().mimeData()
    assert clip.hasText() and not clip.hasHtml() and clip.formats() == ["text/plain"]
    assert ascii(clip.text()) == ascii(whole)
    # cut removes the selection and copies its text
    _select(editor, starts[4], _end(editor))
    editor.cut()
    assert QApplication.clipboard().mimeData().text() == "l" + EMOJI + "ast"
    assert editor.document().findBlockByNumber(4).text() == ""

    # Exit safety. The payload above was made in PYTHON; left on the clipboard at interpreter exit it kills the process
    # (exit 139). Laundering swaps it, losslessly, for a Qt-made copy of the same text; a foreign payload is never touched.
    before = QApplication.clipboard().text()
    assert write_editor.launder_clipboard() is True
    assert QApplication.clipboard().text() == before and QApplication.clipboard().mimeData().formats() == ["text/plain"]
    foreign = QMimeData()
    foreign.setText("t")
    foreign.setHtml("<b>t</b>")
    QApplication.clipboard().setMimeData(foreign)
    assert write_editor.launder_clipboard() is False and QApplication.clipboard().mimeData().hasHtml()
    QApplication.clipboard().clear()
    assert write_editor.launder_clipboard() is False  # an empty clipboard is fine too
    # ...and every editor does it by itself when the application quits (the interpreter is still alive then)
    laundered = []
    monkeypatch.setattr(write_editor, "launder_clipboard", lambda: laundered.append(True) or True)
    qapp.aboutToQuit.emit()
    assert laundered


# -- 8. bold rules -------------------------------------------------------------------------------------------------------------------------


def test_bold_rules_all_mixed_none_and_the_typing_format(qapp):
    original = _doc((0, [Run("plain "), Run("bold", {"bold": True}), Run(" tail")]))
    editor, probe = _make_editor(original)

    # none of it bold -> bold; all of it bold -> off (back to the original)
    _select(editor, 0, 5)
    assert editor.current_tool_state()["bold"] is False
    editor.toggle_bold()
    assert _paragraph(editor).runs == [Run("plain", {"bold": True}), Run(" ", {}), Run("bold", {"bold": True}), Run(" tail", {})]
    assert editor.current_tool_state()["bold"] is True
    editor.toggle_bold()
    assert editor.to_model().paragraphs == original.paragraphs

    # mixed -> apply to ALL (not "toggle each"); then every fragment has it -> remove from all
    _select_all(editor)
    assert editor.current_tool_state()["bold"] is False  # "checked" means ALL selected text has it
    editor.toggle_bold()
    assert _paragraph(editor).runs == [Run("plain bold tail", {"bold": True})]
    editor.toggle_bold()
    assert _paragraph(editor).runs == [Run("plain bold tail", {})]
    # the off-value is written explicitly (a merge cannot delete a property) yet reads back as absent
    fragments = list(block_fragments(editor.document().begin()))
    assert all(f.charFormat().fontWeight() < 600 for f in fragments)
    assert any(f.charFormat().hasProperty(_P.FontWeight) for f in fragments)

    # no selection: the tool toggles the TYPING format, not the document
    end = _end(editor)
    _select(editor, end)
    edits, revision = probe.edits, editor.document().revision()
    editor.toggle_bold()
    assert editor.currentCharFormat().fontWeight() >= 600
    assert editor.current_tool_state()["bold"] is True
    assert probe.edits == edits and editor.document().revision() == revision  # nothing was edited
    QTest.keyClicks(editor, "XY")
    assert _paragraph(editor).runs == [Run("plain bold tail", {}), Run("XY", {"bold": True})]
    editor.toggle_bold()
    assert editor.currentCharFormat().fontWeight() < 600 and editor.current_tool_state()["bold"] is False
    QTest.keyClicks(editor, "Z")
    assert _paragraph(editor).runs == [Run("plain bold tail", {}), Run("XY", {"bold": True}), Run("Z", {})]
    assert probe.errors == []


# -- 9. line and dash ---------------------------------------------------------------------------------------------------------------------------


def test_line_and_dash_apply_to_all_remove_from_all_and_read_back_as_absent(qapp):
    original = _doc(
        (0, [Run("one ", {"bold": True}), Run("two", {"line": True, "dash": True}), Run(" three", {"spread": 2.0})]),
        (1, [Run("four", {"dash": True})]),
    )
    editor, _ = _make_editor(original)
    _select_all(editor)

    editor.toggle_line()  # mixed: apply to every fragment of both paragraphs
    runs = [run for p in editor.to_model().paragraphs for run in p.runs]
    assert all(run.tags.get("line") is True for run in runs)
    assert [run.tags.get("dash") for run in runs] == [None, True, None, True]  # dash untouched
    assert runs[0].tags.get("bold") is True and runs[2].tags.get("spread") == 2.0  # so are the other tags
    assert editor.current_tool_state()["line"] is True

    editor.toggle_line()  # all have it: remove from all
    runs = [run for p in editor.to_model().paragraphs for run in p.runs]
    assert all("line" not in run.tags for run in runs)
    assert [run.tags.get("dash") for run in runs] == [None, True, None, True]

    editor.toggle_dash()  # mixed: everyone gets the diagonal
    assert all(run.tags.get("dash") is True for p in editor.to_model().paragraphs for run in p.runs)
    editor.toggle_dash()  # all have it: none keeps it
    model = editor.to_model()
    assert all("dash" not in run.tags for p in model.paragraphs for run in p.runs)
    assert model.paragraphs[0].runs == [Run("one ", {"bold": True}), Run("two", {}), Run(" three", {"spread": 2.0})]
    # the overlay painter reads the raw property: an explicit off-value must not draw
    for frag in list(block_fragments(editor.document().begin())):
        assert not is_dash(frag.charFormat())
    assert any(f.charFormat().hasProperty(PROP_DASH) for f in block_fragments(editor.document().begin()))

    # typing format: line then dash on a caret
    _select(editor, _end(editor))
    editor.toggle_line()
    editor.toggle_dash()
    QTest.keyClicks(editor, "Q")
    assert _paragraph(editor, 1).runs[-1] == Run("Q", {"line": True, "dash": True})


# -- 10. italics ----------------------------------------------------------------------------------------------------------------------------------


def _italic_of(editor, number=0):
    return [run.tags.get("italic") for run in _paragraph(editor, number).runs]


def test_italic_cycle_mixed_selection_and_ctrl_i(qapp):
    editor, _ = _make_editor(_doc((0, [Run("aaa "), Run("bbb", {"bold": True})])))
    _select_all(editor)

    editor.cycle_italic()  # upright -> lean right (the native italic)
    assert _italic_of(editor) == ["right", "right"] and _paragraph(editor).runs[1].tags["bold"] is True
    fmt = list(block_fragments(editor.document().begin()))[0].charFormat()
    assert fmt.fontItalic() and fmt.intProperty(PROP_LEAN) == 0 and not is_left_lean(fmt)
    assert editor.current_tool_state()["italic"] == "right" and editor.current_tool_state()["italic_right"] is True

    editor.cycle_italic()  # -> lean left (custom painted: the native italic is OFF, the property is on)
    assert _italic_of(editor) == ["left", "left"]
    fmt = list(block_fragments(editor.document().begin()))[0].charFormat()
    assert not fmt.fontItalic() and fmt.intProperty(PROP_LEAN) == LEAN_LEFT and is_left_lean(fmt)
    assert editor.current_tool_state()["italic"] == "left" and editor.current_tool_state()["italic_right"] is False

    editor.cycle_italic()  # -> upright: both off-values written, both read back as absent
    assert _paragraph(editor).runs == [Run("aaa ", {}), Run("bbb", {"bold": True})]
    assert not any(is_left_lean(f.charFormat()) or f.charFormat().fontItalic() for f in block_fragments(editor.document().begin()))

    # mixed selections: the next state comes from the FIRST text fragment, applied to everything
    def cycled(first, second):
        ed, _ = _make_editor(_doc((0, [Run("f", {"italic": first} if first else {}), Run("s", {"italic": second} if second else {})])))
        _select_all(ed)
        ed.cycle_italic()
        return [run.tags.get("italic") for run in _paragraph(ed).runs]

    assert cycled("right", None) == ["left"]  # right -> left, and the upright one follows
    assert cycled(None, "right") == ["right"]  # upright -> right
    assert cycled("left", "right") == [None]  # left -> upright, and the right-leaning one follows (they coalesce)
    assert cycled(None, None) == ["right"]

    # Ctrl+I (decision D5): toggles the right lean only - never the left, never the cycle
    def ctrl_i(first, second):
        ed, _ = _make_editor(_doc((0, [Run("f", {"italic": first} if first else {}), Run("s", {"italic": second} if second else {})])))
        _select_all(ed)
        ed.toggle_italic_right()
        return [run.tags.get("italic") for run in _paragraph(ed).runs]

    assert ctrl_i(None, None) == ["right"]
    assert ctrl_i("right", "right") == [None]
    assert ctrl_i("left", None) == ["right"]  # left is not "all right": everyone leans right
    assert ctrl_i("right", None) == ["right"]  # mixed: apply to all
    assert ctrl_i("left", "left") == ["right"]

    # no selection: the typing format cycles
    typing, _ = _make_editor(_doc((0, [Run("x")])))
    _select(typing, _end(typing))
    typing.cycle_italic()
    QTest.keyClicks(typing, "R")
    typing.cycle_italic()
    QTest.keyClicks(typing, "L")
    typing.cycle_italic()
    QTest.keyClicks(typing, "U")
    assert _paragraph(typing).runs == [Run("x", {}), Run("R", {"italic": "right"}), Run("L", {"italic": "left"}), Run("U", {})]


# -- 11. spread ------------------------------------------------------------------------------------------------------------------------------------------


def _spreads(editor, number=0):
    return [run.tags.get("spread") for run in _paragraph(editor, number).runs]


def test_spread_narrow_clamps_and_is_additive_per_fragment(qapp):
    original = _doc((0, [Run("aa", {"spread": 1.0}), Run("bb", {"bold": True}), Run("cc", {"spread": 2.5})]))
    editor, probe = _make_editor(original)
    _select_all(editor)

    editor.spread_by(SPREAD_STEP)  # every fragment moves by one step: their differences are kept
    assert _spreads(editor) == [2.0, 1.0, 3.5]
    editor.spread_by(-SPREAD_STEP)  # ...and a full step back lands exactly on the start (0 reads as absent)
    assert editor.to_model().paragraphs == original.paragraphs

    # narrow may go below zero (D7: condensed text), down to SPREAD_MIN, then stops
    editor.spread_by(-SPREAD_STEP)
    assert _spreads(editor) == [None, -1.0, 1.5]  # aa: 1 - 1 = 0 reads as absent; bb goes below zero
    for _ in range(10):
        editor.spread_by(-SPREAD_STEP)
    assert _spreads(editor) == [SPREAD_MIN] * 3  # three runs: bb is bold, so they never coalesce
    assert SPREAD_MIN == -3.0
    revision = editor.document().revision()
    probe.statuses.clear()
    editor.spread_by(-SPREAD_STEP)  # at the floor: no change, no undo step, and it says so
    assert editor.document().revision() == revision and "limit" in probe.statuses[-1]

    # ...and up to SPREAD_MAX
    for _ in range(40):
        editor.spread_by(SPREAD_STEP)
    assert _spreads(editor) == [SPREAD_MAX] * 3 and SPREAD_MAX == 32.0
    editor.spread_by(1000.0)
    assert _spreads(editor) == [SPREAD_MAX] * 3
    editor.spread_by(-1000.0)
    assert _spreads(editor) == [SPREAD_MIN] * 3

    # the letter-spacing is the absolute kind, in pixels, on the document format
    fmt = list(block_fragments(editor.document().begin()))[0].charFormat()
    assert fmt.fontLetterSpacingType() == QFont.SpacingType.AbsoluteSpacing and fmt.fontLetterSpacing() == SPREAD_MIN

    # no selection: the typing format widens the next characters
    typing, _ = _make_editor(_doc((0, [Run("x")])))
    _select(typing, _end(typing))
    typing.spread_by(SPREAD_STEP)
    typing.spread_by(SPREAD_STEP)
    QTest.keyClicks(typing, "W")
    assert _paragraph(typing).runs == [Run("x", {}), Run("W", {"spread": 2.0})]
    assert typing.current_tool_state()["spread"] == 2.0


# -- 12. tools skip embeds ------------------------------------------------------------------------------------------------------------------------------------------


_EMBED_TOOL_PROPERTIES = (_P.FontWeight, _P.FontStrikeOut, _P.FontItalic, _P.FontLetterSpacing, _P.FontLetterSpacingType, PROP_DASH, PROP_LEAN)


def _run_every_character_tool(editor):
    """Bold, Line, Dash, the italic cycle, Spread and Ctrl+I (six tools) on the current selection."""
    editor.toggle_bold()
    editor.toggle_line()
    editor.toggle_dash()
    editor.cycle_italic()  # upright -> right
    editor.spread_by(SPREAD_STEP)
    editor.toggle_italic_right()  # all right -> upright again (Ctrl+I toggles)


def _assert_image_untouched(document):
    (image,) = _image_fragments(document)
    fmt = image.charFormat()
    for prop in _EMBED_TOOL_PROPERTIES:
        assert not fmt.hasProperty(prop), prop  # the image format never received a text-tool property


def test_tools_skip_embeds(qapp):
    png = _png()
    source = [Run("Goals: x", {"bold": True})]
    model = _doc((0, [Run("aa "), EmbedRun(png, source), Run(" bb")]))
    editor, probe = _make_editor(model)
    document = editor.document()
    assert ascii(document.toPlainText()) == ascii("aa " + OBJECT_CHAR + " bb")

    _select_all(editor)
    _run_every_character_tool(editor)
    runs = _paragraph(editor).runs
    assert [type(run).__name__ for run in runs] == ["Run", "EmbedRun", "Run"]
    assert runs[1].png == png and runs[1].source == source  # the embed is exactly what it was
    for text_run in (runs[0], runs[2]):  # ...while the text around it took the tools (italic is off again: cycled, then toggled)
        assert text_run.tags == {"bold": True, "line": True, "dash": True, "spread": 1.0}
    _assert_image_untouched(document)
    editor.cycle_italic()  # and the italic on its own reaches the text but not the picture
    assert [run.tags.get("italic") for run in _paragraph(editor).runs if isinstance(run, Run)] == ["right", "right"]
    _assert_image_untouched(document)

    # a selection of ONLY the embed: every character tool is a no-op that says so, and edits nothing
    _select(editor, 3, 4)
    revision, undo_state = document.revision(), document.isUndoAvailable()
    probe.statuses.clear()
    _run_every_character_tool(editor)
    assert document.revision() == revision and document.isUndoAvailable() == undo_state
    assert len(probe.statuses) == 6 and all("only pictures" in message for message in probe.statuses)
    _assert_image_untouched(document)


# -- 13. every tool is exactly one undo step --------------------------------------------------------------------------------------------------------------------------------


def _three_paragraphs() -> WriteDocument:
    return _doc(
        (0, [Run("alpha "), Run("beta", {"bold": True}), Run(" gamma", {"spread": 2.0})]),
        (1, [Run("delta "), Run("eps", {"italic": "left"})]),
        (0, [Run("zeta", {"dash": True}), Run(" eta")]),
    )


_UNDO_TOOLS = {
    "bold": lambda editor: editor.toggle_bold(),
    "line": lambda editor: editor.toggle_line(),
    "dash": lambda editor: editor.toggle_dash(),
    "italic": lambda editor: editor.cycle_italic(),
    "spread": lambda editor: editor.spread_by(SPREAD_STEP),
    "indent": lambda editor: editor.indent_selection(1),
    "embed": lambda editor: editor.embed_selection(),
}


@pytest.mark.parametrize("tool", list(_UNDO_TOOLS))
def test_every_tool_is_exactly_one_undo_step(qapp, tool):
    editor, probe = _make_editor(_three_paragraphs())
    document = editor.document()
    assert not document.isUndoAvailable() and not document.isModified()
    if tool == "embed":
        _select(editor, 2, 13)  # inside the first paragraph only (an embed refuses more than one)
    else:
        _select_all(editor)  # three paragraphs, seven fragments: many edits inside the tool
    before = editor.to_model()
    edits = probe.edits

    _UNDO_TOOLS[tool](editor)
    after = editor.to_model()

    assert after.paragraphs != before.paragraphs, "the tool must have done something"
    assert document.isUndoAvailable() and document.isModified() and probe.edits > edits
    document.undo()  # ONE undo restores everything
    assert editor.to_model().paragraphs == before.paragraphs
    assert not document.isUndoAvailable()
    document.redo()
    assert editor.to_model().paragraphs == after.paragraphs
    assert not document.isRedoAvailable()
    if tool == "embed":
        assert any(isinstance(run, EmbedRun) for run in after.paragraphs[0].runs)
        assert len(editor.embeds) == 1  # append-only: undo did not forget the picture (redo needs it)
    assert probe.errors == []


# -- 14. tool state signal ---------------------------------------------------------------------------------------------------------------------------------------------------------


def test_tool_state_signal_tracks_selection_caret_and_tools(qapp):
    model = _doc((0, [Run("aa", {"bold": True}), Run("bb")]), (2, [Run("cc", {"italic": "right", "spread": 3.0, "dash": True})]))
    editor, probe = _make_editor(model)
    assert len(probe.states) == 1  # the load reports the state once (the caret sits on the bold "aa")
    probe.states.clear()
    starts = _starts(editor)

    _select(editor, 2, 4)  # "bb": nothing on
    assert probe.states[-1] == {"bold": False, "line": False, "dash": False, "italic": None, "italic_right": False, "spread": 0.0, "indent": 0}
    _select(editor, 0, 2)  # "aa": all bold
    assert probe.states[-1] == {"bold": True, "line": False, "dash": False, "italic": None, "italic_right": False, "spread": 0.0, "indent": 0}
    _select(editor, 0, 4)  # "checked" = ALL selected text has it: bb does not
    assert probe.states[-1]["bold"] is False
    _select(editor, starts[1] + 1)  # a caret inside the indented, leaning, spread, dashed paragraph
    state = probe.states[-1]
    assert state["indent"] == 2 and state["italic"] == "right" and state["italic_right"] is True
    assert state["spread"] == 3.0 and state["dash"] is True and state["bold"] is False
    assert editor.current_tool_state() == state

    # a selection across both paragraphs: indent is the one at the selection start, dash is not everywhere
    _select(editor, 1, starts[1] + 2)
    assert probe.states[-1]["indent"] == 0 and probe.states[-1]["dash"] is False

    # a tool changes the state with the selection unchanged
    _select(editor, 2, 4)
    assert probe.states[-1]["bold"] is False
    editor.toggle_bold()
    assert probe.states[-1]["bold"] is True
    editor.cycle_italic()
    assert probe.states[-1]["italic"] == "right"
    editor.indent_selection(1)
    assert probe.states[-1]["indent"] == 1
    # typing-format tools show on the caret
    _select(editor, 4)
    editor.toggle_line()
    assert probe.states[-1]["line"] is True

    # emitted only on change: no two consecutive identical states, and every one is a complete dict
    assert len(probe.states) >= 8
    assert all(a != b for a, b in zip(probe.states, probe.states[1:]))
    assert all(set(s) == {"bold", "line", "dash", "italic", "italic_right", "spread", "indent"} for s in probe.states)
    # an untouched no-op does not re-emit
    count = len(probe.states)
    editor.current_tool_state()
    editor.setTextCursor(editor.textCursor())
    assert len(probe.states) == count


# -- 15. indent range rules ------------------------------------------------------------------------------------------------------------------------------------------------------------------------


def test_indent_range_rules_including_block_start_exclusion(qapp):
    editor, _ = _make_editor(_doc((0, [Run("one")]), (0, [Run("two")]), (0, [Run("three")]), (0, [Run("four")])))
    starts = _starts(editor)

    def indents():
        return [p.indent for p in editor.to_model().paragraphs]

    _select(editor, starts[1] + 1)  # a caret: its own block
    editor.indent_selection(1)
    assert indents() == [0, 1, 0, 0]

    _select(editor, starts[1] + 1, starts[2] + 2)  # mid block 1 to mid block 2: both
    editor.indent_selection(1)
    assert indents() == [0, 2, 1, 0]

    _select(editor, starts[0] + 1, starts[2])  # ends EXACTLY at block 2's start: block 2 is not included
    editor.indent_selection(1)
    assert indents() == [1, 3, 1, 0]

    _select(editor, starts[0] + len("one"), starts[1] + 2)  # starts at the END of block 0: block 0 counts
    editor.indent_selection(1)
    assert indents() == [2, 4, 1, 0]

    _select(editor, starts[2], starts[3])  # a whole line plus its break: only that line
    editor.indent_selection(1)
    assert indents() == [2, 4, 2, 0]
    assert editor.textCursor().selectionStart() == starts[2] and editor.textCursor().selectionEnd() == starts[3]  # selection kept

    _select_all(editor)
    editor.indent_selection(1)
    assert indents() == [3, 5, 3, 1]
    assert editor.textCursor().selectionStart() == 0 and editor.textCursor().selectionEnd() == _end(editor)

    editor.indent_selection(-1)  # outdent follows the same range rules
    assert indents() == [2, 4, 2, 0]
    _select(editor, starts[2], _end(editor))
    editor.indent_selection(-1)
    assert indents() == [2, 4, 1, 0]  # the last block is already at 0 and stays there

    # an embed-only paragraph indents like any other
    with_embed, _ = _make_editor(_doc((0, [EmbedRun(_png(), [Run("Goals: x")])]), (0, [Run("after")])))
    _select(with_embed, 0, 1)
    with_embed.indent_selection(2)
    assert [p.indent for p in with_embed.to_model().paragraphs] == [2, 0]
    assert isinstance(with_embed.to_model().paragraphs[0].runs[0], EmbedRun)


# -- 16. caps and floor -----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------


def test_indent_caps_at_max_indent_and_floors_at_zero(qapp):
    editor, probe = _make_editor(_doc((0, [Run("x")])))
    document = editor.document()
    assert MAX_INDENT == 32

    editor.indent_selection(-1)  # the floor: nothing changes, not even the undo stack, and it says so
    assert _paragraph(editor).indent == 0 and not document.isUndoAvailable()
    assert "left edge" in probe.statuses[-1]

    for _ in range(MAX_INDENT + 10):
        editor.indent_selection(1)
    assert _paragraph(editor).indent == MAX_INDENT
    assert "deepest" in probe.statuses[-1]
    revision = document.revision()
    editor.indent_selection(1)  # the cap: a no-op
    assert document.revision() == revision and _paragraph(editor).indent == MAX_INDENT
    editor.indent_selection(1000)
    assert _paragraph(editor).indent == MAX_INDENT
    editor.indent_selection(-1000)
    assert _paragraph(editor).indent == 0
    editor.indent_selection(5)
    editor.indent_selection(-2)
    assert _paragraph(editor).indent == 3

    # the indent survives a save-shaped round trip (model -> document -> model)
    editor.indent_selection(MAX_INDENT)
    saved = editor.to_model()
    editor.load_model(saved)
    assert _paragraph(editor).indent == MAX_INDENT
    assert editor.document().findBlockByNumber(0).blockFormat().indent() == MAX_INDENT


# -- 17. keys ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------


def test_tab_shift_tab_and_enter_keep_indent(qapp):
    editor, probe = _make_editor(_doc((0, [Run("Phrase: x")])))
    _select(editor, _end(editor))

    QTest.keyClick(editor, Qt.Key.Key_Tab)
    assert _paragraph(editor).indent == 1
    QTest.keyClick(editor, Qt.Key.Key_Tab)
    assert _paragraph(editor).indent == 2
    assert editor.toPlainText() == "Phrase: x", "Tab indents; it never types a tab character"
    QTest.keyClick(editor, Qt.Key.Key_Backtab, Qt.KeyboardModifier.ShiftModifier)  # Shift+Tab as Qt delivers it
    assert _paragraph(editor).indent == 1
    QTest.keyClick(editor, Qt.Key.Key_Tab, Qt.KeyboardModifier.ShiftModifier)  # ...and as some platforms do
    assert _paragraph(editor).indent == 0
    QTest.keyClick(editor, Qt.Key.Key_Backtab, Qt.KeyboardModifier.ShiftModifier)  # the floor
    assert _paragraph(editor).indent == 0
    assert "\t" not in editor.toPlainText()

    # Enter inherits the indent (and so does typing on the new line)
    QTest.keyClick(editor, Qt.Key.Key_Tab)
    QTest.keyClick(editor, Qt.Key.Key_Tab)
    QTest.keyClick(editor, Qt.Key.Key_Return)
    QTest.keyClicks(editor, "next")
    assert [(p.indent, p.runs[0].text) for p in editor.to_model().paragraphs] == [(2, "Phrase: x"), (2, "next")]

    # Shift+Enter is a plain Enter: no U+2028 soft break hiding a second line inside one paragraph
    QTest.keyClick(editor, Qt.Key.Key_Return, Qt.KeyboardModifier.ShiftModifier)
    QTest.keyClicks(editor, "more")
    assert editor.document().blockCount() == 3 and LINE_SEP not in editor.toPlainText()
    assert [p.indent for p in editor.to_model().paragraphs] == [2, 2, 2]

    # Shift+Enter over a selection replaces it, like Enter
    _select(editor, 0, len("Phrase"))
    QTest.keyClick(editor, Qt.Key.Key_Return, Qt.KeyboardModifier.ShiftModifier)
    assert editor.document().findBlockByNumber(0).text() == "" and editor.document().findBlockByNumber(1).text() == ": x"

    # Tab over a selection indents every touched block and keeps the selection
    _select_all(editor)
    QTest.keyClick(editor, Qt.Key.Key_Tab)
    assert [p.indent for p in editor.to_model().paragraphs] == [3, 3, 3, 3]
    assert editor.textCursor().selectionStart() == 0 and editor.textCursor().selectionEnd() == _end(editor)
    # Ctrl+Tab is not ours (no indent change)
    QTest.keyClick(editor, Qt.Key.Key_Tab, Qt.KeyboardModifier.ControlModifier)
    assert [p.indent for p in editor.to_model().paragraphs] == [3, 3, 3, 3]
    assert probe.errors == []


# -- 18. content_edited ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------


def test_content_edited_fires_on_real_edits_only(qapp, tmp_path):
    root_file = write_testkit.make_write(tmp_path / ("main" + write_paths.ext_for("write")), ["Info: x"])
    model = _doc((0, [Run("Info: @gone.txt")]), (0, [Run("Goals: x")]), (0, [Run("# note")]))
    editor, probe = _make_editor(model, root_file=str(root_file))
    document = editor.document()
    document.contentsChanged.connect(probe.on_contents_changed)
    editor.textChanged.connect(probe.on_text_changed)
    assert probe.edits == 0, "loading a model is not a user edit"

    # everything the refresh machinery does re-highlights: none of it is an edit
    editor.highlighter.rehighlight()
    editor.highlighter.invalidate()
    editor.set_root_file(str(root_file))
    editor.set_root_file(None)
    editor.set_root_file(str(root_file))
    assert probe.edits == 0
    # ...although the signals a careless hook would pick DO fire (the reason the hook is contentsChange)
    assert probe.text_changed >= 1 and probe.contents_changed >= 1

    # real edits: typing, a format tool, an indent, a paste, an undo
    QTest.keyClicks(editor, "ab")
    assert probe.edits == 2
    _select(editor, 0, 4)
    editor.toggle_bold()
    assert probe.edits == 3
    editor.indent_selection(1)
    assert probe.edits == 4
    mime = QMimeData()
    mime.setText("zz")
    editor.insertFromMimeData(mime)
    assert probe.edits == 5
    editor.document().undo()
    assert probe.edits == 6
    # a tool that changes nothing is not an edit (first go to the floor, which may be a real edit)
    _select(editor, 0)
    editor.indent_selection(-1000)
    edits = probe.edits
    editor.indent_selection(-1)
    editor.indent_selection(-1000)
    assert probe.edits == edits

    # loads and clears are not edits; the caret-only typing-format toggle is not either
    editor.load_model(_doc((0, [Run("other")])))
    editor.clear_all()
    editor.toggle_bold()
    assert probe.edits == edits


# -- 19. completion ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------


def test_completion_popup_accepts_in_one_undo_step(qapp):
    editor, probe = _make_editor(_doc((0, [])))
    document = editor.document()
    popup = editor.completer.popup()
    assert not popup.isVisible()

    QTest.keyClicks(editor, "Ph")
    assert popup.isVisible() and editor.completer.completionPrefix() == "Ph"
    assert popup.currentIndex().data() == "Phrase"
    QTest.keyClick(popup, Qt.Key.Key_Tab)  # Tab accepts the completion (it does not indent)
    assert editor.toPlainText() == "Phrase: " and not popup.isVisible()
    assert _paragraph(editor).indent == 0
    assert editor.textCursor().position() == len("Phrase: ")
    edits = probe.edits
    document.undo()  # ONE undo step takes the whole insertion back to what was typed
    assert editor.toPlainText() == "Ph"
    document.redo()
    assert editor.toPlainText() == "Phrase: "
    assert probe.edits > edits

    # case-insensitive, Enter accepts too, and the typed case is replaced by the canonical name
    editor.clear_all()
    QTest.keyClicks(editor, "kn")
    assert popup.isVisible()
    QTest.keyClick(popup, Qt.Key.Key_Return)
    assert editor.toPlainText() == "Knowledge: " and editor.document().blockCount() == 1  # Enter did not also break the line

    # Escape closes the popup and leaves the text alone
    editor.clear_all()
    QTest.keyClicks(editor, "Ph")
    assert popup.isVisible()
    QTest.keyClick(popup, Qt.Key.Key_Escape)
    assert not popup.isVisible() and editor.toPlainText() == "Ph"

    # typing on: a colon, a space or a non-matching letter closes it; Backspace re-opens it
    editor.clear_all()
    QTest.keyClicks(editor, "Info")
    assert popup.isVisible()
    QTest.keyClicks(editor, ":")
    assert not popup.isVisible() and editor.toPlainText() == "Info:"
    editor.clear_all()
    QTest.keyClicks(editor, "Pz")
    assert not popup.isVisible()
    QTest.keyClick(editor, Qt.Key.Key_Backspace)
    assert popup.isVisible() and editor.toPlainText() == "P"
    QTest.keyClick(editor, Qt.Key.Key_Backspace)
    assert not popup.isVisible()

    # only a partial keyword at the very start of a line completes
    editor.clear_all()
    QTest.keyClicks(editor, "x Ph")
    assert not popup.isVisible()
    editor.clear_all()
    QTest.keyClicks(editor, "Goals: fi")
    assert not popup.isVisible()
    editor.clear_all()
    QTest.keyClick(editor, Qt.Key.Key_Return)
    QTest.keyClicks(editor, "Re")
    assert popup.isVisible()  # a fresh line completes again
    popup.hide()

    # without a popup Tab is the indent key (covered by the keys test) and the popup never shows for a selection
    editor.clear_all()
    QTest.keyClicks(editor, "Un")
    assert popup.isVisible()
    _select(editor, 0, 1)
    QTest.keyClick(editor, Qt.Key.Key_Shift)
    assert not popup.isVisible()

    # accepting a completion outside a keyword prefix does nothing
    editor.clear_all()
    QTest.keyClicks(editor, "12")
    editor._insert_completion("Phrase")
    assert editor.toPlainText() == "12"
    editor._insert_completion("NotAKeyword")
    assert editor.toPlainText() == "12"
    assert write_grammar.matching_keywords("Ph") == ["Phrase"]


# -- 20. insert reference ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------


def test_insert_reference(qapp):
    editor, probe = _make_editor(_doc((0, [Run("Info:")]), (0, []), (0, [Run("Goals: old")])))
    starts = _starts(editor)
    document = editor.document()

    _select(editor, starts[0] + len("Info:"))  # after "Info:" a space is added
    assert editor.insert_reference("sub/a.txt") is True
    assert document.findBlockByNumber(0).text() == "Info: @sub/a.txt"
    assert editor.textCursor().position() == starts[0] + len("Info: @sub/a.txt")

    starts = _starts(editor)
    _select(editor, starts[1])  # an empty line: just the reference (no keyword is invented); backslashes normalise
    assert editor.insert_reference("sub dir\\it's.txt") is True
    assert document.findBlockByNumber(1).text() == "@sub dir/it's.txt"

    starts = _starts(editor)
    _select(editor, starts[2] + len("Goals: "), starts[2] + len("Goals: old"))  # replaces the selection
    assert editor.insert_reference("./x/../y.txt") is True
    assert document.findBlockByNumber(2).text() == "Goals: @x/../y.txt"
    assert editor.insert_reference(".") is True  # the folder itself
    assert document.findBlockByNumber(2).text() == "Goals: @x/../y.txt@."

    # one undo step per insertion
    document.undo()
    assert document.findBlockByNumber(2).text() == "Goals: @x/../y.txt"
    document.undo()
    assert document.findBlockByNumber(2).text() == "Goals: old"

    # a reference the syntax check refuses is not inserted; the document is untouched and the error is reported
    before = (editor.toPlainText(), document.revision())
    refused = ["", "   ", "C:\\Windows\\win.ini", "\\\\host\\share\\x", "/abs/path", "a.txt:stream", "CON", "bad\nname", "a" * 2000, "x<y"]
    for bad in refused:
        assert editor.insert_reference(bad) is False, ascii(bad)
    assert (editor.toPlainText(), document.revision()) == before
    assert len(probe.errors) == len(refused) and all("Cannot insert" in message for message in probe.errors)

    # what was inserted is a real reference line for the grammar
    info = write_grammar.classify_line(document.findBlockByNumber(0).text())
    assert info.kind == "instruction" and info.is_ref and info.ref_text == "sub/a.txt"


# -- 21. load_model / clear_all ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------


def test_load_model_and_clear_all_reset_the_session(qapp):
    editor, probe = _make_editor()
    document = editor.document()
    png = _png()
    model = _doc((1, [Run("Phrase: ", {"bold": True}), EmbedRun(png, [Run("Info: x")])]), (0, [Run("z\tq", {"italic": "left"})]))

    assert editor.load_model(model) == []
    assert editor.to_model().paragraphs == model.paragraphs
    assert editor.outline() == model.outline() == [(1, "Phrase: Info: x"), (0, "z\tq")]
    assert len(editor.embeds) == 1 and len(_image_fragments(document)) == 1
    assert not document.isModified() and not document.isUndoAvailable() and probe.edits == 0
    assert editor.textCursor().position() == 0 and not editor.textCursor().hasSelection()

    # editing goes on normally afterwards; the highlighter survived the load and colours new lines. (Re-attaching
    # a QSyntaxHighlighter queues its own full pass, and until the loop has run it it ignores typed changes.)
    QTest.qWait(20)
    _select(editor, _end(editor))
    QTest.keyClick(editor, Qt.Key.Key_Return)  # never `keyClicks(editor, "\n...")`: see the module docstring
    QTest.keyClicks(editor, "Goals: x")
    assert probe.edits > 0 and document.isModified() and document.isUndoAvailable()
    assert editor.highlighter.document() is document
    last = document.lastBlock()
    assert last.text() == "Goals: x" and len(last.layout().formats()) == 2  # keyword + value colours

    # a second load replaces everything: registry emptied (the session is over), history wiped, edits not counted
    edits = probe.edits
    assert editor.load_model(_doc((0, [Run("second")]))) == []
    assert len(editor.embeds) == 0 and _image_fragments(document) == []
    assert editor.toPlainText() == "second" and not document.isUndoAvailable() and not document.isModified()
    assert probe.edits == edits

    # the tool state follows the load
    assert probe.states[-1]["indent"] == 0 and probe.states[-1]["bold"] is False
    editor.load_model(model)
    assert probe.states[-1]["indent"] == 1

    # a corrupt picture degrades to its source text with a warning (the words are never lost)
    corrupt = _doc((0, [Run("a "), EmbedRun(write_testkit.fake_png_header(20, 20), [Run("keep me")])]))
    warnings = editor.load_model(corrupt)
    assert len(warnings) == 1 and "source text" in warnings[0]
    assert editor.toPlainText() == "a keep me" and len(editor.embeds) == 0

    # clear_all: one empty paragraph, no history, nothing modified
    editor.clear_all()
    assert editor.toPlainText() == "" and document.blockCount() == 1 and _paragraph(editor).indent == 0
    assert editor.to_model().paragraphs == WriteDocument().paragraphs
    assert not document.isUndoAvailable() and not document.isModified() and probe.edits == edits

    # embed / un-embed round trip through the editor API, refusals reported as status (not error)
    editor.load_model(_doc((0, [Run("Goals: keep "), Run("this", {"bold": True})])))
    _select(editor, 0, 5)
    probe.statuses.clear()
    assert editor.embed_selection() is True and len(editor.embeds) == 1
    assert ascii(editor.toPlainText()) == ascii(OBJECT_CHAR + ": keep this")
    assert editor.outline() == [(0, "Goals: keep this")]
    _select(editor, 1)  # caret just after the picture
    assert editor.unembed_selection() is True
    assert editor.toPlainText() == "Goals: keep this"
    assert editor.unembed_selection() is False and "Place the cursor next to an embed" in probe.statuses[-1]
    _select(editor, 0, _end(editor) - 1)
    starts_before = editor.toPlainText()
    editor.document().setPlainText(editor.toPlainText() + "\nsecond line")  # two paragraphs
    _select(editor, 2, _end(editor) - 2)  # a multi-paragraph selection cannot be embedded (D4)
    assert editor.embed_selection() is False and "one paragraph" in probe.statuses[-1]
    assert probe.errors == [] and starts_before == "Goals: keep this"


# -- 22. paint: the overlays sit over the text of an INDENTED paragraph -----------------------------------------------------------------------------------------------------------------------------------------------------------


def _indent_overlay_editor(indent, tags):
    editor, _ = _make_editor(
        _doc((indent, [Run("MMMM", tags)])),
        size=(360, 120),
        point_size=24,
        palette=_palette(),
    )
    editor.setFrameShape(QTextEdit.Shape.NoFrame)
    editor.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    editor.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    _paint_text_red(editor)
    return editor


def _x_range(points):
    xs = [x for x, _ in points]
    return min(xs), max(xs)


def test_overlays_sit_over_the_text_of_an_indented_paragraph(qapp):
    flat = _grab(_indent_overlay_editor(0, {}))
    control = _grab(_indent_overlay_editor(2, {}))
    flat_red = _ink_points(flat, _reddish)
    control_red = _ink_points(control, _reddish)
    if not control_red or not flat_red:
        pytest.skip("platform drew no text ink")
    assert not _ink_points(control, _dark), "control: with no Dash and no lean there is no overlay ink"
    red_lo, red_hi = _x_range(control_red)
    # sanity: two indent levels really move the text by about two indent widths (24 px each)
    assert _x_range(flat_red)[0] + 2 * 24 == pytest.approx(red_lo, abs=3)

    # Dash: the diagonal spans the run - it must not sit 24 px per indent level to the LEFT of it
    dashed = _grab(_indent_overlay_editor(2, {"dash": True}))
    dark = _ink_points(dashed, _dark)
    assert dark, "the Dash diagonal must have been painted"
    dark_lo, dark_hi = _x_range(dark)
    assert abs(dark_lo - red_lo) <= 8 and abs(dark_hi - red_hi) <= 8, (dark_lo, dark_hi, red_lo, red_hi)
    assert len(_ink_points(dashed, _reddish)) >= 0.8 * len(control_red)  # the text itself is still there

    # Left lean: covers and redraws over the text (a tilt moves the top left by at most ~0.3 x the line height)
    leaning = _grab(_indent_overlay_editor(2, {"italic": "left"}))
    lean_dark = _ink_points(leaning, _dark)
    assert lean_dark, "the sheared redraw must be painted"
    lean_lo, lean_hi = _x_range(lean_dark)
    assert red_lo - 20 <= lean_lo and lean_hi <= red_hi + 8, (lean_lo, lean_hi, red_lo, red_hi)
    centroid = sum(x for x, _ in lean_dark) / len(lean_dark)
    control_centroid = sum(x for x, _ in control_red) / len(control_red)
    assert abs(centroid - control_centroid) <= 16, (centroid, control_centroid)
    assert len(_ink_points(leaning, _reddish)) / len(control_red) < 0.05, "the cover must hide the upright glyphs"


# -- 23. paint: the caret is redrawn over a left-lean cover ------------------------------------------------------------------------------------------------------------------------------------------------------------------


def test_caret_is_redrawn_over_a_lean_cover(qapp):
    # A run of TABS draws no ink of its own, so the cover is blank Base and the only difference between the
    # two images is the caret the editor puts back (Qt itself draws none: the widget really has no focus).
    editor, _ = _make_editor(
        _doc((0, [Run("\t\t", {"italic": "left"})])),
        cls=_FocusedEditor,
        size=(360, 120),
        point_size=24,
        palette=_palette(),
    )
    editor.setFrameShape(QTextEdit.Shape.NoFrame)
    editor.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    editor.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    editor.setCursorWidth(2)
    _select(editor, 1)  # between the two tabs: strictly inside the cover
    unfocused = _grab(editor)
    caret = editor.cursorRect()
    assert not _ink_points(unfocused, _dark), "nothing but blank Base is painted in the lean cover"

    editor.focused = True
    focused = _grab(editor)
    diff = [(x, y) for x, y, r, g, b in _pixels(focused) if focused.pixel(x, y) != unfocused.pixel(x, y)]
    assert diff, "a focused caret inside a cover must be redrawn"
    assert all(caret.x() - 1 <= x <= caret.x() + 2 and caret.y() - 1 <= y <= caret.y() + caret.height() for x, y in diff), (diff[:5], caret)
    assert len({x for x, _ in diff}) == 2  # cursorWidth() columns
    assert len({y for _, y in diff}) >= 0.8 * caret.height()
    assert all(_dark(*rgb) for rgb in ((focused.pixel(x, y) >> 16 & 255, focused.pixel(x, y) >> 8 & 255, focused.pixel(x, y) & 255) for x, y in diff))

    # with a selection the caret is not redrawn (the selection is the cue)
    cursor = editor.textCursor()
    cursor.setPosition(0)
    cursor.setPosition(2, QTextCursor.MoveMode.KeepAnchor)
    editor.setTextCursor(cursor)
    selected = _grab(editor)
    assert len(_ink_points(selected, _dark)) == 0


# -- 24. paint: an overlay failure is printed once and never raised --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------


def test_overlay_exception_is_printed_once_and_never_raised(qapp, monkeypatch, capsys):
    editor, _ = _make_editor(_doc((0, [Run("x", {"dash": True})])), size=(200, 80))

    def boom(*args, **kwargs):
        raise RuntimeError("overlay exploded")

    monkeypatch.setattr(write_editor, "paint_overlays", boom)
    _grab(editor)
    _grab(editor)
    editor.repaint()
    captured = capsys.readouterr()
    assert captured.err.count("overlay painting failed") == 1
    assert "overlay exploded" in captured.err
    monkeypatch.undo()
    assert _ink_points(_grab(editor), _dark) is not None  # painting still works afterwards; the editor is intact
