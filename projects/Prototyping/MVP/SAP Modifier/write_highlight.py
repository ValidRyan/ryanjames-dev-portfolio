"""Syntax highlighter for the SAP Modifier: instruction lines and live reference state.

`WriteHighlighter(QSyntaxHighlighter)` colours what the instruction grammar
(`write_grammar.classify_line`) says a line is, and - for `Keyword: @path`
lines - what the path policy (`write_paths.classify_ref`) says the target is:

- comment (`# ...`)   -> dim grey, the whole line;
- instruction         -> the keyword in one colour, a plain-text value in another;
- reference           -> the value (including its `@`) by state: ok = teal,
  broken = red + wavy underline, blocked = orange + wavy underline, unsaved =
  grey-blue + dotted underline;
- noise and blank     -> no format at all;
- a line containing U+FFFC (an embedded picture) -> no format: the picture is
  the colour, and the words underneath are not what the user sees there.

Relations: `write_grammar` (spans and line kinds) and `write_paths` (state) only;
no other Write module. `write_editor.py` attaches one to its document and calls
`set_root_file` / `invalidate`; `write_docmodel.model_to_document` detaches and
re-attaches it around a load.

Hard rule (a test enforces it): the highlighter sets FOREGROUND colour and
UNDERLINE only - never a background, weight, italic, letter/word spacing,
strike-out or font family. Reasons: the overlay painter (`write_overlay.py`)
covers left-lean and Dash runs with the palette Base colour and redraws them, so
an invisible-glyph trick or a background here would fight it; and bold / italic /
spacing are the user's own tools, which the document model reads back from the
DOCUMENT formats - highlighter formats live in `QTextLayout.formats()` and must
never look like a tag.

Reference state is classified once per distinct reference text and cached
("per pass"): typing a line re-highlights one block, a full `rehighlight()` walks
every block, and neither may `lstat` the same path over and over. The cache is
dropped by `set_root_file` (a different folder means different answers) and by
`invalidate()`, which the window calls on activation and after a save - the two
moments a file can have appeared or vanished behind the highlighter's back.
Between those calls a colour can be stale by design (documented choice: cheap
beats live; the resolver tree has the authoritative state). The cache is also
capped (`MAX_CACHE`) so typing many distinct references cannot grow it forever.

Refresh hooks must use `QTextDocument.contentsChange`, never `contentsChanged` /
`textChanged` / `revision()`: `rehighlight()` fires the latter (verified, fact
V5) and would feed a refresh loop; it does NOT fire `contentsChange(int,int,int)`
(`test_write_highlight.py` is the canary).

Documented choices: references on the root Write's own lines are relative to the
real folder of the root file (`root_real`, computed once per `set_root_file`),
with no root file (`None`) every well-formed reference is `unsaved`; character
offsets handed to `setFormat` are UTF-16 code units (Qt's), so spans are
converted when the text holds characters outside the Basic Multilingual Plane;
the highlighter never raises out of `highlightBlock`.

Explicitly out of scope: resolving nested Writes (`write_resolve.py`), counting
lines, autocomplete, and any file access beyond the `lstat` walk `classify_ref`
does.
"""

import os

from PySide6.QtGui import QBrush, QColor, QSyntaxHighlighter, QTextCharFormat

import write_paths
from write_grammar import classify_line

OBJECT_CHAR = "\N{OBJECT REPLACEMENT CHARACTER}"
MAX_CACHE = 4096

_NO_UNDERLINE = QTextCharFormat.UnderlineStyle.NoUnderline
_WAVE = QTextCharFormat.UnderlineStyle.WaveUnderline
_DOTTED = QTextCharFormat.UnderlineStyle.DotLine

COLOR_COMMENT = QColor(128, 128, 128)
COLOR_KEYWORD = QColor(112, 48, 160)
COLOR_VALUE = QColor(21, 101, 192)

# state -> (foreground colour, underline style); one row per `classify_ref` result.
REF_STYLES = {
    "ok": (QColor(0, 128, 128), _NO_UNDERLINE),
    "broken": (QColor(200, 32, 32), _WAVE),
    "blocked": (QColor(224, 112, 0), _WAVE),
    "unsaved": (QColor(96, 122, 160), _DOTTED),
}


def _foreground_format(color: QColor, underline=_NO_UNDERLINE) -> QTextCharFormat:
    """A format holding ONLY a foreground colour and (optionally) an underline style."""
    fmt = QTextCharFormat()
    fmt.setForeground(QBrush(color))
    if underline != _NO_UNDERLINE:
        fmt.setUnderlineStyle(underline)
    return fmt


def _utf16_units(text: str) -> int:
    """Length of `text` in UTF-16 code units (what `QString` indexes count)."""
    return len(text.encode("utf-16-le", "surrogatepass")) // 2


class WriteHighlighter(QSyntaxHighlighter):
    """Colours instruction lines and reference states; see the module docstring.

    Construct with the document to highlight (`WriteHighlighter(editor.document())`);
    like any `QSyntaxHighlighter` it is a child of the document and dies with it.
    `root_file` is the path of the root Write on disk or None (unsaved).
    """

    def __init__(self, document, root_file=None):
        # Plain attributes first: Qt schedules a first pass as soon as the base
        # class is attached to a document, and `highlightBlock` needs all of these.
        self._comment_format = _foreground_format(COLOR_COMMENT)
        self._keyword_format = _foreground_format(COLOR_KEYWORD)
        self._value_format = _foreground_format(COLOR_VALUE)
        self._ref_formats = {state: _foreground_format(color, style) for state, (color, style) in REF_STYLES.items()}
        self._cache: dict[str, str] = {}
        self._root_real: str | None = None
        self._root_file = None
        self._compute_root(root_file)
        super().__init__(document)

    # -- configuration -----------------------------------------------------

    @property
    def root_real(self) -> str | None:
        """The link-free folder every reference is resolved against, or None when unsaved."""
        return self._root_real

    def _compute_root(self, root_file) -> None:
        self._root_file = root_file
        self._root_real = None
        if root_file is None:
            return
        try:
            real_file = os.path.realpath(root_file)
            self._root_real = write_paths.real_root(os.path.dirname(real_file))
        except (OSError, ValueError):
            self._root_real = None  # an unusable path reads as "unsaved", never as a crash

    def set_root_file(self, root_file) -> None:
        """Point the highlighter at a (new) root Write, drop the cache and re-highlight.

        `root_file` is a path or None. `root_real` is computed here, once, so the
        per-block work is only `classify_ref`.
        """
        self._compute_root(root_file)
        self._cache.clear()
        self.rehighlight()

    def invalidate(self) -> None:
        """Forget every cached reference state and re-highlight (window activation, after a save)."""
        self._cache.clear()
        self.rehighlight()

    # -- classification ----------------------------------------------------

    def _ref_state(self, ref_text: str) -> str:
        state = self._cache.get(ref_text)
        if state is None:
            try:
                state = write_paths.classify_ref(ref_text, self._root_real, self._root_real)
            except (OSError, ValueError):
                state = "broken"
            if len(self._cache) >= MAX_CACHE:
                self._cache.clear()
            self._cache[ref_text] = state
        return state

    def _paint(self, text: str, span, fmt: QTextCharFormat) -> None:
        """Apply `fmt` to the `(start, end)` span (code-point offsets into `text`)."""
        if span is None:
            return
        start, end = span
        if end <= start:
            return
        if text.isascii():
            self.setFormat(start, end - start, fmt)
        else:
            head = _utf16_units(text[:start])
            self.setFormat(head, _utf16_units(text[start:end]), fmt)

    # -- QSyntaxHighlighter ------------------------------------------------

    def highlightBlock(self, text: str) -> None:
        """Format one block (see the module docstring for the rules)."""
        if not text or OBJECT_CHAR in text:
            return
        info = classify_line(text)
        if info.kind == "comment":
            self._paint(text, (0, len(text)), self._comment_format)
            return
        if info.kind != "instruction":
            return
        self._paint(text, info.kw_span, self._keyword_format)
        if info.value_span is None:
            return
        if info.is_ref:
            fmt = self._ref_formats.get(self._ref_state(info.ref_text or ""), self._ref_formats["broken"])
            self._paint(text, info.value_span, fmt)
        else:
            self._paint(text, info.value_span, self._value_format)
