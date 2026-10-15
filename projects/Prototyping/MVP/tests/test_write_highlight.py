"""Tests for write_highlight.py - the instruction / reference syntax highlighter.

Highlighter formats live in `QTextLayout.formats()` (an overlay the document's own
fragments never see), so every colour assertion reads `block.layout().formats()`
after a synchronous `rehighlight()`. A bare `QTextDocument` is enough for those; the
one test about `contentsChange` (the V5 canary) uses a real `QTextEdit`, because
`contentsChange` is only emitted for a document that has a layout. Reference states
use REAL files in `tmp_path` (ok / broken / blocked) and a `None` root (unsaved).

Every test runs under the `write_qt` fixture and registers the widgets it builds with
`write_testkit.track_qt` (see write_testkit's Qt lifetime rules). Source stays ASCII:
the object-replacement character and non-BMP characters are built with `chr()` /
named escapes and printed strings go through `ascii()`.
"""

import sys
from pathlib import Path

_MVP_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_MVP_ROOT / "SAP Modifier"))

import pytest
from PySide6.QtCore import QBuffer, QIODevice, Qt
from PySide6.QtGui import QColor, QImage, QTextCharFormat, QTextCursor, QTextDocument, QTextFormat
from PySide6.QtWidgets import QTextEdit

import write_highlight
import write_paths
import write_testkit
from write_docmodel import model_to_document
from write_highlight import COLOR_COMMENT, COLOR_KEYWORD, COLOR_VALUE, REF_STYLES, WriteHighlighter
from write_model import EmbedRegistry, EmbedRun, Paragraph, Run, WriteDocument
from write_testkit import write_qt  # noqa: F401  (fixture: see write_testkit's Qt lifetime rules)

pytestmark = pytest.mark.usefixtures("write_qt")

OBJECT_CHAR = chr(0xFFFC)
NO_UNDERLINE = QTextCharFormat.UnderlineStyle.NoUnderline


def _rgb(color: QColor) -> tuple:
    return (color.red(), color.green(), color.blue())


def _ranges(block) -> list:
    """`[(start, length, (r, g, b), underline_style)]` of the highlighter formats on `block`."""
    out = []
    for item in block.layout().formats():
        fmt = item.format
        out.append((item.start, item.length, _rgb(fmt.foreground().color()), fmt.underlineStyle()))
    return out


def _block(document: QTextDocument, number: int):
    return document.findBlockByNumber(number)


def _highlighted(lines, root_file=None):
    """A bare document holding `lines`, with a highlighter that has already run once."""
    document = QTextDocument()
    document.setPlainText("\n".join(lines))
    highlighter = WriteHighlighter(document, root_file)
    highlighter.rehighlight()
    return document, highlighter


def _png(width=6, height=4) -> bytes:
    image = QImage(width, height, QImage.Format.Format_ARGB32)
    image.fill(Qt.GlobalColor.red)
    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    assert image.save(buffer, "PNG")
    return bytes(buffer.data())


def _root_with_file(tmp_path):
    """A saved root Write plus one real sibling file; returns `(root_file, real_file)`."""
    root_file = write_testkit.make_write(tmp_path / ("main" + write_paths.ext_for("write")), ["Info: x"])
    real_file = tmp_path / "here.txt"
    real_file.write_text("hello", encoding="utf-8")
    return str(root_file), real_file


# -- 1. keyword / value / comment colours ---------------------------------------


def test_keyword_value_and_comment_colours(qapp):
    astral = chr(0x1F600)  # one code point, two UTF-16 units: Qt offsets must count the two
    lines = [
        "Goals: finish the draft",  # 0: keyword + text value
        "# a note",  # 1: comment
        "phrase:tight",  # 2: case-insensitive keyword, no space after the colon
        "Just some prose",  # 3: noise
        "",  # 4: blank
        "Info:",  # 5: instruction with an empty value: keyword only
        "Goals : x",  # 6: a space before the colon is noise
        "Understanding:   spaced   ",  # 7: the value is trimmed
        "Goals: " + astral + " ok",  # 8: a non-BMP character inside the value
    ]
    document, highlighter = _highlighted(lines)
    kw, value, comment = _rgb(COLOR_KEYWORD), _rgb(COLOR_VALUE), _rgb(COLOR_COMMENT)

    assert _ranges(_block(document, 0)) == [(0, 5, kw, NO_UNDERLINE), (7, 16, value, NO_UNDERLINE)]
    assert _ranges(_block(document, 1)) == [(0, len("# a note"), comment, NO_UNDERLINE)]
    assert _ranges(_block(document, 2)) == [(0, 6, kw, NO_UNDERLINE), (7, 5, value, NO_UNDERLINE)]
    assert _ranges(_block(document, 3)) == []
    assert _ranges(_block(document, 4)) == []
    assert _ranges(_block(document, 5)) == [(0, 4, kw, NO_UNDERLINE)]
    assert _ranges(_block(document, 6)) == []
    assert _ranges(_block(document, 7)) == [(0, 13, kw, NO_UNDERLINE), (17, 6, value, NO_UNDERLINE)]
    # the three colours are told apart, or the test above would prove nothing
    assert len({kw, value, comment}) == 3
    # UTF-16 units: "Goals: " is 7, the astral character 2, " ok" 3 - the value covers 5 units, not 4
    assert _block(document, 8).length() - 1 == len("Goals: ") + 2 + len(" ok")
    assert _ranges(_block(document, 8)) == [(0, 5, kw, NO_UNDERLINE), (7, 5, value, NO_UNDERLINE)]
    assert highlighter.document() is document


# -- 2. reference states -----------------------------------------------------------


def _ref_ranges(ref_chars: int, state: str, keyword: str = "Info") -> list:
    """The two ranges of `<keyword>: @...`: the keyword, then the `@path` value in `state`'s look."""
    color, style = REF_STYLES[state]
    return [(0, len(keyword), _rgb(COLOR_KEYWORD), NO_UNDERLINE), (len(keyword) + 2, ref_chars, _rgb(color), style)]


def test_reference_states_have_their_own_colour_and_underline(qapp, tmp_path):
    root_file, _ = _root_with_file(tmp_path)
    ok_line = "Info: @here.txt"
    broken_line = "Info: @gone.txt"
    escape_line = "Info: @../outside.txt"
    drive_line = "Info: @C:\\Windows\\win.ini"
    unc_line = "Info: @\\\\host\\share\\x"
    empty_line = "Info: @"
    lines = [ok_line, broken_line, escape_line, drive_line, unc_line, empty_line]
    document, highlighter = _highlighted(lines, root_file)

    assert _ranges(_block(document, 0)) == _ref_ranges(len("@here.txt"), "ok")
    assert _ranges(_block(document, 1)) == _ref_ranges(len("@gone.txt"), "broken")
    assert _ranges(_block(document, 2)) == _ref_ranges(len("@../outside.txt"), "blocked")
    assert _ranges(_block(document, 3)) == _ref_ranges(len("@C:\\Windows\\win.ini"), "blocked")
    assert _ranges(_block(document, 4)) == _ref_ranges(len("@\\\\host\\share\\x"), "blocked")
    assert _ranges(_block(document, 5)) == _ref_ranges(1, "broken")  # an empty reference is red, never plain text
    assert highlighter.root_real is not None

    # every state has its own look: distinct colours, and the underline says how bad it is
    looks = {state: (_rgb(color), style) for state, (color, style) in REF_STYLES.items()}
    assert len({rgb for rgb, _ in looks.values()}) == 4
    assert looks["ok"][1] == NO_UNDERLINE
    assert looks["broken"][1] == looks["blocked"][1] == QTextCharFormat.UnderlineStyle.WaveUnderline
    assert looks["unsaved"][1] == QTextCharFormat.UnderlineStyle.DotLine

    # no root file: every well-formed reference is "unsaved" (even one that would escape - only the
    # syntax check comes first); a syntactically hostile one stays "blocked"; empty stays "broken"
    highlighter.set_root_file(None)
    assert highlighter.root_real is None
    assert _ranges(_block(document, 0)) == _ref_ranges(len("@here.txt"), "unsaved")
    assert _ranges(_block(document, 1)) == _ref_ranges(len("@gone.txt"), "unsaved")
    assert _ranges(_block(document, 2)) == _ref_ranges(len("@../outside.txt"), "unsaved")
    assert _ranges(_block(document, 3)) == _ref_ranges(len("@C:\\Windows\\win.ini"), "blocked")
    assert _ranges(_block(document, 4)) == _ref_ranges(len("@\\\\host\\share\\x"), "blocked")
    assert _ranges(_block(document, 5)) == _ref_ranges(1, "broken")

    # an ok reference does not have to be a file: a folder is fine, something missing below it is not
    (tmp_path / "folder").mkdir()
    document2, _ = _highlighted(["Phrase: @folder", "Phrase: @folder/nothing"], root_file)
    assert _ranges(_block(document2, 0)) == _ref_ranges(len("@folder"), "ok", "Phrase")
    assert _ranges(_block(document2, 1)) == _ref_ranges(len("@folder/nothing"), "broken", "Phrase")


# -- 3. only foreground colour and underline, ever -----------------------------------


_PROP = QTextFormat.Property
_ALLOWED_PROPERTIES = {int(_PROP.ForegroundBrush), int(_PROP.FontUnderline), int(_PROP.TextUnderlineStyle)}
_FORBIDDEN_PROPERTIES = (
    _PROP.BackgroundBrush,
    _PROP.FontWeight,
    _PROP.FontItalic,
    _PROP.FontLetterSpacing,
    _PROP.FontLetterSpacingType,
    _PROP.FontWordSpacing,
    _PROP.FontStrikeOut,
    _PROP.FontOverline,
    _PROP.FontFamilies,
    _PROP.FontPointSize,
    _PROP.FontPixelSize,
    _PROP.FontCapitalization,
    _PROP.FontKerning,
    _PROP.FontStretch,
    _PROP.FontFixedPitch,
    _PROP.TextOutline,
    _PROP.TextVerticalAlignment,
)


def test_highlighter_never_sets_background_weight_italic_or_spacing(qapp, tmp_path):
    root_file, _ = _root_with_file(tmp_path)
    lines = [
        "Goals: text value",
        "# comment",
        "Info: @here.txt",
        "Info: @gone.txt",
        "Info: @../outside.txt",
        "Info: @",
        "prose that is noise",
        "",
        "Understanding: " + chr(0x1F600),
    ]
    checked = 0
    for root in (root_file, None):  # None adds the "unsaved" look
        document, _ = _highlighted(lines, root)
        block = document.begin()
        while block.isValid():
            for item in block.layout().formats():
                fmt = item.format
                assert set(fmt.properties()) <= _ALLOWED_PROPERTIES, (block.text(), sorted(fmt.properties()))
                for prop in _FORBIDDEN_PROPERTIES:
                    assert not fmt.hasProperty(prop), (block.text(), prop)
                assert fmt.hasProperty(_PROP.ForegroundBrush)
                # the values read back as "unset": bold would be >= 600 (the document model's threshold)
                assert fmt.fontWeight() == 400 and not fmt.fontItalic() and not fmt.fontStrikeOut()
                assert fmt.fontLetterSpacing() == 0.0
                checked += 1
            block = block.next()
    assert checked >= 20, "the scan must have looked at real formats"


# -- 4. a paragraph holding a picture is not coloured --------------------------------


def test_embed_paragraph_is_uncoloured(qapp, tmp_path):
    root_file, _ = _root_with_file(tmp_path)
    picture = _png()
    model = WriteDocument(
        [
            Paragraph(0, [Run("Goals: plain")]),
            Paragraph(0, [Run("Phrase: "), EmbedRun(picture, [Run("Goals: x")])]),
            Paragraph(0, [Run("# comment "), EmbedRun(picture, [Run("Goals: x")])]),
            Paragraph(0, [Run("Info: @here.txt"), EmbedRun(picture, [Run("Goals: x")])]),
            Paragraph(0, [Run("Info: @here.txt")]),
            Paragraph(0, [EmbedRun(picture, [Run("Goals: x")])]),
        ]
    )
    document = QTextDocument()
    highlighter = WriteHighlighter(document, root_file)
    warnings = model_to_document(model, document, EmbedRegistry(), highlighter=highlighter)
    assert warnings == []
    highlighter.rehighlight()

    for number in (1, 2, 3, 5):
        assert OBJECT_CHAR in _block(document, number).text(), number
        assert _ranges(_block(document, number)) == [], f"paragraph {number} holds a picture, so it must have no colour"
    # controls: the same words without a picture are coloured, so the picture really is the switch
    assert _ranges(_block(document, 0)) != []
    assert _ranges(_block(document, 4)) == _ref_ranges(len("@here.txt"), "ok")


# -- 5. the V5 canary: rehighlight() must not emit contentsChange -------------------


class _Recorder:
    """Plain Python receiver (no QObject, no closure over Qt state) for document signals."""

    def __init__(self):
        self.content_changes = []
        self.contents_changed = 0
        self.text_changed = 0

    def on_contents_change(self, position, removed, added):
        self.content_changes.append((position, removed, added))

    def on_contents_changed(self):
        self.contents_changed += 1

    def on_text_changed(self):
        self.text_changed += 1


def test_rehighlight_emits_no_contents_change(qapp, tmp_path):
    """Fact V5: hooking `contentsChange` is safe, hooking `contentsChanged` / `textChanged` is a loop.

    `contentsChange` is only emitted for a document that has a layout, so this uses a real
    `QTextEdit`. If a Qt upgrade ever makes a rehighlight emit it, the refresh debounce
    built on that signal would feed itself - this test is what would say so.
    """
    root_file, _ = _root_with_file(tmp_path)
    edit = write_testkit.track_qt(QTextEdit())
    document = edit.document()
    edit.setPlainText("Goals: a\nInfo: @here.txt\n# note")
    highlighter = WriteHighlighter(document, root_file)
    recorder = _Recorder()
    document.contentsChange.connect(recorder.on_contents_change)
    document.contentsChanged.connect(recorder.on_contents_changed)
    edit.textChanged.connect(recorder.on_text_changed)

    revision = document.revision()
    highlighter.rehighlight()
    assert recorder.content_changes == []
    # ...while the two signals a careless hook would pick DO fire (why the plan names contentsChange)
    assert recorder.contents_changed >= 1 and recorder.text_changed >= 1
    assert document.revision() > revision

    highlighter.invalidate()
    highlighter.set_root_file(root_file)
    highlighter.set_root_file(None)
    assert recorder.content_changes == []

    # the canary is alive: a real edit does emit it, once, with removed + added > 0
    end = document.characterCount() - 1
    cursor = QTextCursor(document)
    cursor.movePosition(QTextCursor.MoveOperation.End)
    cursor.insertText("z")
    assert recorder.content_changes == [(end, 0, 1)]
    # and typing re-highlighted that block: the comment colour now spans the longer line
    last = document.lastBlock()
    assert last.text() == "# notez"
    assert _ranges(last) == [(0, len("# notez"), _rgb(COLOR_COMMENT), NO_UNDERLINE)]


# -- 6. reclassification when the disk changes (after invalidate) ---------------------


def test_reclassifies_when_a_file_appears_after_invalidate(qapp, tmp_path, monkeypatch):
    root_file, _ = _root_with_file(tmp_path)
    late = tmp_path / "late.txt"
    lines = ["Info: @late.txt", "Goals: @late.txt", "Info: @here.txt"]
    document, highlighter = _highlighted(lines, root_file)
    broken_first = _ref_ranges(len("@late.txt"), "broken")
    ok_first = _ref_ranges(len("@late.txt"), "ok")
    assert _ranges(_block(document, 0)) == broken_first

    # the file appears; the per-pass cache still says broken (documented: cheap beats live)...
    late.write_text("now here", encoding="utf-8")
    highlighter.rehighlight()
    assert _ranges(_block(document, 0)) == broken_first
    # ...until the window is activated or a save happens: invalidate() drops the cache
    highlighter.invalidate()
    assert _ranges(_block(document, 0)) == ok_first
    assert _ranges(_block(document, 1))[1][2] == _rgb(REF_STYLES["ok"][0])

    # and back again when it disappears
    late.unlink()
    highlighter.invalidate()
    assert _ranges(_block(document, 0)) == broken_first

    # the cache holds ONE answer per distinct reference text: three blocks, two texts, two lookups
    calls = []
    real = write_paths.classify_ref

    def counting(raw, base_dir, root_real):
        calls.append(raw)
        return real(raw, base_dir, root_real)

    monkeypatch.setattr(write_paths, "classify_ref", counting)
    highlighter.invalidate()
    assert sorted(calls) == ["here.txt", "late.txt"]
    highlighter.rehighlight()
    assert len(calls) == 2, "a plain rehighlight reuses the cache"
    highlighter.invalidate()
    assert len(calls) == 4

    # a different root folder means different answers: set_root_file drops the cache too
    other = tmp_path / "other"
    other.mkdir()
    other_root = write_testkit.make_write(other / ("main" + write_paths.ext_for("write")), ["Info: x"])
    (other / "late.txt").write_text("elsewhere", encoding="utf-8")
    monkeypatch.setattr(write_paths, "classify_ref", real)
    highlighter.set_root_file(str(other_root))
    assert _ranges(_block(document, 0)) == ok_first  # late.txt exists in the new folder
    assert _ranges(_block(document, 2)) == _ref_ranges(len("@here.txt"), "broken")  # here.txt does not
    assert highlighter.root_real == write_paths.real_root(str(other))

    # the cache is bounded
    monkeypatch.setattr(write_highlight, "MAX_CACHE", 3)
    for n in range(10):
        highlighter._ref_state(f"name{n}.txt")
    assert len(highlighter._cache) <= 3
