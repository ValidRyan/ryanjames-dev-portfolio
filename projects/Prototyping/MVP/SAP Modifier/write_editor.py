"""The SAP Modifier text editor: `WriteEditor(QTextEdit)` with the seven tools (hazards C, D, E).

One widget that owns everything a Write needs at the text level: the seven tools
(Bold, Line, Dash, Italics, Spread, Embed, Indent), the paste / drop / copy guard,
the keyword completer, the keyboard rules (Tab / Shift+Tab / Enter) and the overlay
painting of Dash and left lean. It sits on top of the modules that already exist and
re-implements none of them:

    write_docmodel   tags <-> QTextCharFormat, document <-> model (`set_tag`,
                     `tags_from_char_format`, `document_to_model`, `model_to_document`)
    write_overlay    `paint_overlays`: the Dash diagonal and the left lean
    write_embed      `embed_range` / `unembed_ranges` / `embed_image_for_url`
    write_highlight  `WriteHighlighter` (instruction / reference colours)
    write_grammar    the eight keywords and the completion helpers
    write_model      the tag algebra (`toggle_target`, `next_italic`, `apply_spread`, ...),
                     `sanitize_text` and the session `EmbedRegistry`
    write_paths      `check_ref_syntax`, used by `insert_reference`

Signals: `content_edited()` (a REAL edit: hooked to `QTextDocument.contentsChange`
with `removed + added > 0`, never to `textChanged` / `contentsChanged` / `revision`,
because a syntax-highlighter `rehighlight()` fires those and would feed a refresh loop
- fact V5, `test_write_highlight.py` is the canary), `tool_state_changed(dict)` (what
the toolbar shows), `status_message(str)` (a refusal or hint the user should read) and
`error_message(str)` (something failed; the window shows it with an `Error:` prefix).

Hazard C - clipboard and drop (decision D9: plain text only). `setAcceptRichText(False)`
alone is not enough: a default paste of `<img src="file://host/x.png">` made Qt request
the URL (fact 4). So `canInsertFromMimeData` answers `hasText()`, `insertFromMimeData`
inserts SANITISED plain text and never reads the HTML, `createMimeDataFromSelection`
puts plain text on the clipboard (an embed becomes its source text) and `loadResource`
serves one thing only: a `mem://<id>` picture the session registry holds. Nothing here
ever calls `setHtml` / `insertHtml`; the base-class `loadResource` (the file / network
loader) is never called.

Clipboard lifetime trap (found by running the suite): a `QMimeData` built in PYTHON and
left on the clipboard when the interpreter exits kills the process (exit 139 / access
violation) - Qt flushes the clipboard while Python is already finalising and calls back
into the Python-side object. `createMimeDataFromSelection` has to return such an object,
so on `QApplication.aboutToQuit` (the interpreter is still fully alive) every editor calls
`launder_clipboard()`, which swaps a plain-text payload for `QClipboard.setText`'s
Qt-made copy of the same text: lossless, because our payload is plain text only, and a
foreign payload (anything with more formats than text/plain) is never touched. A
process that exits WITHOUT `aboutToQuit` (a test run) must call it, or
`QClipboard.clear()`, itself.

Tool rules (all in one place): bold / line / dash toggle by `toggle_target` over the
selection's text fragments (apply to all if any lacks it, remove only if all have it);
with no selection the tool toggles the TYPING format (`mergeCurrentCharFormat`).
Italics take the next state from the FIRST text fragment and apply it to the whole
selection; Ctrl+I (`toggle_italic_right`, decision D5) only toggles the right lean.
Spread adds `+/-SPREAD_STEP` per fragment through `apply_spread`, so different
fragments keep their differences. A picture (embed) is skipped by every tool. EVERY
tool is one `beginEditBlock` / `endEditBlock` on one cursor, so exactly one undo step.
Off-values are written explicitly (`set_tag` overwrites, it does not delete) and read
back as absent tags, because `mergeCharFormat` cannot clear a property.

Documented choices (the plan leaves them open):
- The plan names a `delta_format` helper in `write_docmodel`; it does not exist, so
  the one-tag delta formats are built with `set_tag(QTextCharFormat(), key, value)`.
- Shift+Enter is treated as Enter. Qt's default inserts U+2028 (a soft line break
  inside ONE paragraph), which would show two lines but count as one and be flattened
  to a space on save.
- `load_model` and `clear_all` also empty the embed registry: they replace the whole
  session and wipe the undo history, so no undo can ask for an old picture, and opening
  file after file must not exhaust `MAX_EMBEDS`. During a load `content_edited` is
  suppressed (the load is not a user edit).
- Paste is also cut at the paragraph budget (`MAX_PARAGRAPHS`): a document over the cap
  could not be saved, so the editor never lets a paste create one.
- Refusals ("select text inside one paragraph", "place the cursor next to an embed")
  arrive as `status_message`; a genuine failure arrives as `error_message`.
- `insert_reference(rel)` inserts `@rel` at the caret (replacing the selection), with a
  space first when the caret follows `Keyword:`. It invents no default keyword; a
  reference the syntax check refuses is not inserted (`error_message`). It returns
  whether it inserted.
- The tool state is `{bold, line, dash}` (True when ALL selected text has it),
  `italic` (the FIRST fragment's state: what the next cycle step advances from),
  `italic_right` (all selected text leans right: the Ctrl+I checked state), `spread`
  (the first fragment's letter-spacing, 0.0 when none) and `indent` (the block at the
  selection start). It is emitted only when it changed.
- The completer shows only for a partial keyword at the very start of a line
  (`keyword_prefix_before_cursor`), never during an IME preedit, and accepting inserts
  `Name: ` in one edit block (one undo step).

Explicitly out of scope: the toolbar, file dialogs and saving (`app_write.py`), the
resolver tree (`write_tree.py`), and any file access.
"""

import sys

from PySide6.QtCore import QMimeData, QPointF, QRectF, QStringListModel, Qt, QUrl, Signal
from PySide6.QtGui import QPainter, QPalette, QTextCharFormat, QTextCursor, QTextDocument
from PySide6.QtWidgets import QApplication, QCompleter, QTextEdit

import write_paths
from write_docmodel import (
    EMBED_URL_SCHEME,
    INDENT_WIDTH_PX,
    block_fragments,
    document_to_model,
    embed_id_of,
    model_to_document,
    runs_in_range,
    set_tag,
    tags_from_char_format,
)
from write_embed import EmbedRefused, embed_image_for_url, embed_range, embed_target_range, unembed_ranges
from write_grammar import INSTRUCTION_NAMES, keyword_prefix_before_cursor, matching_keywords
from write_highlight import WriteHighlighter
from write_model import (
    MAX_INDENT,
    MAX_PARAGRAPHS,
    EmbedRegistry,
    WriteDocument,
    apply_spread,
    clamp_indent,
    next_italic,
    runs_text,
    sanitize_text,
    toggle_target,
)
from write_overlay import paint_overlays

MAX_PASTE_CHARS = 200_000

_NOTHING_TO_FORMAT = "Nothing to format: the selection holds only pictures."


def sanitize_paste(text: str) -> str:
    """Make pasted / dropped text safe to insert: capped, controls stripped, breaks normalised.

    Cuts the text to `MAX_PASTE_CHARS` characters first (so the work is bounded), then
    applies `write_model.sanitize_text(keep_newlines=True)`: U+2028 / U+2029 / VT / FF /
    NEL / CR / CRLF become one newline each (a soft break would hide a second line
    inside one paragraph), U+FFFC (what an embed looks like) and every C0 / C1 control
    except tab are removed, and lone surrogates become U+FFFD.
    """
    return sanitize_text(text[:MAX_PASTE_CHARS], keep_newlines=True)


def launder_clipboard() -> bool:
    """Swap a plain-text clipboard payload for a Qt-made copy of it; True when it swapped.

    See "Clipboard lifetime trap" in the module docstring. Only a payload whose ONLY
    format is text/plain is touched (that is exactly what `createMimeDataFromSelection`
    makes), and it is replaced by the same text through `QClipboard.setText`, so nothing
    is lost. Safe to call at any time and any number of times.
    """
    clipboard = QApplication.clipboard()
    data = clipboard.mimeData()
    if data is None or data.formats() != ["text/plain"]:
        return False
    text = data.text()
    clipboard.setText(text)  # deletes the old payload: `data` must not be used again
    return True


class WriteEditor(QTextEdit):
    """The Write text editor; see the module docstring for the rules it enforces.

    `root_file` is the path of the root Write on disk or None (unsaved); the syntax
    highlighter classifies references against its folder. Construct it, then use
    `load_model` / `to_model` to move a `WriteDocument` in and out.
    """

    content_edited = Signal()
    tool_state_changed = Signal(dict)
    status_message = Signal(str)
    error_message = Signal(str)

    def __init__(self, parent=None, root_file=None):
        # Plain attributes first: Qt may call a virtual (`loadResource`, ...) while it is constructed.
        self._embeds = EmbedRegistry()
        self._root_file = root_file
        self._loading = False
        self._last_tool_state = None
        self._overlay_error_reported = False
        self._preedit = False
        super().__init__(parent)

        # Configuration: plain text only, no auto lists, no stylesheet (so the palette Base
        # really is the background the overlay painter covers with).
        self.setAcceptRichText(False)
        self.setAutoFormatting(QTextEdit.AutoFormattingFlag.AutoNone)
        self.setTabChangesFocus(False)
        self.document().setIndentWidth(INDENT_WIDTH_PX)

        self._highlighter = WriteHighlighter(self.document(), root_file)

        # Keyword completer. Parented to the editor (it and its popup die with it).
        self._completer = QCompleter(self)
        self._completer.setModel(QStringListModel(list(INSTRUCTION_NAMES), self._completer))
        self._completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self._completer.setCompletionMode(QCompleter.CompletionMode.PopupCompletion)
        self._completer.setWidget(self)
        self._completer.popup()  # build the popup now, so "is it visible" never creates one
        self._completer.activated.connect(self._insert_completion)

        # Bound methods only (never lambdas over self): see write_testkit's lifetime rules.
        self.document().contentsChange.connect(self._on_contents_change)
        self.currentCharFormatChanged.connect(self._on_tool_state_source)
        self.cursorPositionChanged.connect(self._on_tool_state_source)
        self.selectionChanged.connect(self._on_tool_state_source)
        app = QApplication.instance()
        if app is not None:
            app.aboutToQuit.connect(self._on_about_to_quit)

    # -- attributes for the window and the tests --------------------------------------

    @property
    def highlighter(self) -> WriteHighlighter:
        """The syntax highlighter (the window calls `invalidate()` on activation and save)."""
        return self._highlighter

    @property
    def embeds(self) -> EmbedRegistry:
        """The session embed registry (append-only until a load or `clear_all`)."""
        return self._embeds

    @property
    def completer(self) -> QCompleter:
        """The keyword completer."""
        return self._completer

    @property
    def root_file(self):
        """The root Write's path, or None when unsaved."""
        return self._root_file

    # -- model in / out ---------------------------------------------------------------

    def to_model(self) -> WriteDocument:
        """Read the document into a `WriteDocument` (document formats only)."""
        return document_to_model(self.document(), self._embeds)

    def outline(self) -> list[tuple[int, str]]:
        """`[(indent, logical_text)]` per paragraph - what the grammar / resolver / Run see."""
        return self.to_model().outline()

    def load_model(self, model: WriteDocument) -> list[str]:
        """Replace the contents with `model`; return the load warnings.

        `model_to_document` detaches the highlighter for the build and re-attaches it
        (one pass at the end), disables undo while it works and leaves the document
        clean. The registry is emptied first and `content_edited` is suppressed: a load
        is not a user edit. Qt runs the re-attached highlighter's full pass on the NEXT
        event-loop turn (until then the colours are absent and typed edits are not
        highlighted), so code that inspects colours right after a load must let the loop
        run once (`QTest.qWait`) or call `highlighter.rehighlight()`.
        """
        self._loading = True
        try:
            self._embeds.clear()
            warnings = model_to_document(model, self.document(), self._embeds, highlighter=self._highlighter)
            self.setTextCursor(QTextCursor(self.document()))
        finally:
            self._loading = False
        self._emit_tool_state()
        return warnings

    def clear_all(self) -> None:
        """Back to one empty paragraph with no history (the New action)."""
        self.load_model(WriteDocument())

    def set_root_file(self, root_file) -> None:
        """Point the highlighter at a (new) root Write, or None for unsaved."""
        self._root_file = root_file
        self._highlighter.set_root_file(root_file)

    # -- clipboard, drop and resources (hazard C) ---------------------------------------

    def canInsertFromMimeData(self, source) -> bool:
        """Accept only data that carries text (a picture-only drop is refused)."""
        return source is not None and source.hasText()

    def insertFromMimeData(self, source) -> None:
        """Insert the SANITISED plain text of `source` (paste and drop both land here).

        The HTML, image and URL payloads are never looked at: `source.text()` is the only
        thing read (for a URL-only drop Qt returns the URL strings, which go in as text).
        """
        if source is None or not source.hasText():
            return
        raw = source.text()
        text = sanitize_paste(raw)
        notes = []
        if len(raw) > MAX_PASTE_CHARS:
            notes.append(f"Pasted text was cut to {MAX_PASTE_CHARS} characters.")
        room = max(0, MAX_PARAGRAPHS - self.document().blockCount())
        if text.count("\n") > room:
            cut = -1
            for _ in range(room + 1):
                cut = text.index("\n", cut + 1)
            text = text[:cut]
            notes.append(f"Pasted text was cut at the {MAX_PARAGRAPHS}-paragraph limit.")
        if text:
            self.insertPlainText(text)
        for note in notes:
            self.status_message.emit(note)

    def createMimeDataFromSelection(self) -> QMimeData:
        """The clipboard / drag payload: plain text only, embeds replaced by their source text."""
        mime = QMimeData()
        cursor = self.textCursor()
        if cursor.hasSelection():
            mime.setText(self._plain_text_of(cursor.selectionStart(), cursor.selectionEnd()))
        return mime

    def loadResource(self, type_, name):
        """Serve `mem://<id>` pictures the registry holds - and nothing else, ever.

        No file, no network, no base-class fallback: anything but an image resource with
        the `mem` scheme and a known id answers None.
        """
        if int(type_) != int(QTextDocument.ResourceType.ImageResource):
            return None
        if not isinstance(name, QUrl) or name.scheme() != EMBED_URL_SCHEME:
            return None
        return embed_image_for_url(self._embeds, name)

    def _plain_text_of(self, lo: int, hi: int) -> str:
        """The logical text of document positions `[lo, hi)`: paragraphs joined by newlines."""
        document = self.document()
        lines = []
        block = document.findBlock(lo)
        while block.isValid() and block.position() <= hi:
            start = max(lo, block.position())
            end = min(hi, block.position() + block.length() - 1)
            parts = []
            if end > start:
                for frag in block_fragments(block):
                    a = max(start, frag.position())
                    b = min(end, frag.position() + frag.length())
                    if b <= a:
                        continue
                    fmt = frag.charFormat()
                    if fmt.isImageFormat():
                        record = self._embeds.get(embed_id_of(fmt) or "")
                        if record is not None:
                            parts.append(runs_text(record.source) * (b - a))
                    else:
                        parts.append("".join(run.text for run in runs_in_range(document, a, b)))
            lines.append("".join(parts))
            block = block.next()
        return "\n".join(lines)

    # -- selection reading ----------------------------------------------------------------

    def _selection_pieces(self, lo: int, hi: int) -> list:
        """`[(start, end, tags)]` for every text-fragment piece inside `[lo, hi)`; pictures skipped.

        Fragments never include a paragraph separator, so each piece lies inside one
        block and its absolute positions stay valid while other pieces are reformatted.
        """
        pieces = []
        block = self.document().findBlock(lo)
        while block.isValid() and block.position() < hi:
            for frag in block_fragments(block):
                start = max(lo, frag.position())
                end = min(hi, frag.position() + frag.length())
                if end <= start:
                    continue
                fmt = frag.charFormat()
                if fmt.isImageFormat():
                    continue  # every tool skips embeds
                pieces.append((start, end, tags_from_char_format(fmt)))
            block = block.next()
        return pieces

    def _selected_pieces(self):
        """The pieces of the current selection, or None when nothing is selected."""
        cursor = self.textCursor()
        if not cursor.hasSelection():
            return None
        return self._selection_pieces(cursor.selectionStart(), cursor.selectionEnd())

    def _typing_tags(self) -> dict:
        return tags_from_char_format(self.currentCharFormat())

    # -- tool state ---------------------------------------------------------------------------

    def current_tool_state(self) -> dict:
        """What the toolbar shows for the current selection (or caret); see the module docstring."""
        cursor = self.textCursor()
        pieces = self._selected_pieces()
        tag_sets = [self._typing_tags()] if pieces is None else [tags for _start, _end, tags in pieces]
        first = tag_sets[0] if tag_sets else {}

        def everywhere(key: str) -> bool:
            return bool(tag_sets) and all(tags.get(key) for tags in tag_sets)

        block = self.document().findBlock(cursor.selectionStart())
        return {
            "bold": everywhere("bold"),
            "line": everywhere("line"),
            "dash": everywhere("dash"),
            "italic": first.get("italic"),
            "italic_right": bool(tag_sets) and all(tags.get("italic") == "right" for tags in tag_sets),
            "spread": first.get("spread", 0.0),
            "indent": clamp_indent(block.blockFormat().indent()),
        }

    def _emit_tool_state(self) -> None:
        state = self.current_tool_state()
        if state != self._last_tool_state:
            self._last_tool_state = state
            self.tool_state_changed.emit(state)

    def _on_tool_state_source(self, *_args) -> None:
        """currentCharFormatChanged / cursorPositionChanged / selectionChanged all land here."""
        if not self._loading:
            self._emit_tool_state()

    def _on_about_to_quit(self) -> None:
        """Make the clipboard exit-safe while Python is still alive (see `launder_clipboard`)."""
        launder_clipboard()

    def _on_contents_change(self, position: int, removed: int, added: int) -> None:
        """The ONE hook for "the user edited": `contentsChange`, never textChanged (fact V5)."""
        if self._loading:
            return
        if removed + added > 0:
            self.content_edited.emit()
        self._emit_tool_state()

    # -- tools: character formatting ----------------------------------------------------------

    def _merge_edits(self, edits) -> None:
        """Merge `[(start, end, delta_format)]` in ONE edit block on ONE cursor (one undo step)."""
        cursor = QTextCursor(self.document())
        cursor.beginEditBlock()
        try:
            for start, end, delta in edits:
                cursor.setPosition(start)
                cursor.setPosition(end, QTextCursor.MoveMode.KeepAnchor)
                cursor.mergeCharFormat(delta)
        finally:
            cursor.endEditBlock()

    def _toggle_flag(self, key: str) -> None:
        pieces = self._selected_pieces()
        if pieces is None:  # no selection: toggle what the next typed character gets
            target = toggle_target([bool(self._typing_tags().get(key))])
            self.mergeCurrentCharFormat(set_tag(QTextCharFormat(), key, target))
        elif not pieces:
            self.status_message.emit(_NOTHING_TO_FORMAT)
            return
        else:
            target = toggle_target(bool(tags.get(key)) for _start, _end, tags in pieces)
            delta = set_tag(QTextCharFormat(), key, target)
            self._merge_edits([(start, end, delta) for start, end, _tags in pieces])
        self._emit_tool_state()

    def toggle_bold(self) -> None:
        """Bold on the selection (all bold -> off, otherwise all bold) or the typing format."""
        self._toggle_flag("bold")

    def toggle_line(self) -> None:
        """Strike-out on the selection (same rule as bold) or the typing format."""
        self._toggle_flag("line")

    def toggle_dash(self) -> None:
        """The Dash diagonal on the selection (same rule as bold) or the typing format."""
        self._toggle_flag("dash")

    def _set_italic(self, choose) -> None:
        """Apply the italic state `choose(pieces_or_typing_tags)` picks to the selection / typing format."""
        pieces = self._selected_pieces()
        if pieces is None:
            self.mergeCurrentCharFormat(set_tag(QTextCharFormat(), "italic", choose([self._typing_tags()])))
        elif not pieces:
            self.status_message.emit(_NOTHING_TO_FORMAT)
            return
        else:
            delta = set_tag(QTextCharFormat(), "italic", choose([tags for _start, _end, tags in pieces]))
            self._merge_edits([(start, end, delta) for start, end, _tags in pieces])
        self._emit_tool_state()

    def cycle_italic(self) -> None:
        """Upright -> lean right -> lean left -> upright, from the FIRST text fragment, for the whole selection."""
        self._set_italic(lambda tag_sets: next_italic(tag_sets[0].get("italic")))

    def toggle_italic_right(self) -> None:
        """Ctrl+I (decision D5): all leaning right -> upright, otherwise lean right (never left)."""
        self._set_italic(lambda tag_sets: "right" if toggle_target(t.get("italic") == "right" for t in tag_sets) else None)

    def spread_by(self, delta: float) -> None:
        """Add `delta` px of letter-spacing to every text fragment (clamped by `apply_spread`)."""
        pieces = self._selected_pieces()
        if pieces is None:
            value = apply_spread(self._typing_tags().get("spread"), delta)
            self.mergeCurrentCharFormat(set_tag(QTextCharFormat(), "spread", value))
        elif not pieces:
            self.status_message.emit(_NOTHING_TO_FORMAT)
            return
        else:
            edits = []
            for start, end, tags in pieces:
                value = apply_spread(tags.get("spread"), delta)
                if value != float(tags.get("spread") or 0.0):
                    edits.append((start, end, set_tag(QTextCharFormat(), "spread", value)))
            if not edits:
                self.status_message.emit("Spread is already at its limit.")
                return
            self._merge_edits(edits)
        self._emit_tool_state()

    # -- tools: embed, indent, reference --------------------------------------------------------

    def embed_selection(self) -> bool:
        """Replace the selection (or the current paragraph) with a picture of it; True on success."""
        lo, hi = embed_target_range(self)
        try:
            embed_range(self, lo, hi, self._embeds)
        except EmbedRefused as exc:
            self.status_message.emit(str(exc))
            return False
        except ValueError as exc:
            self.error_message.emit(f"Embed failed: {exc}")
            return False
        return True

    def unembed_selection(self) -> bool:
        """Turn the pictures at / around the caret back into their text; True on success."""
        try:
            unembed_ranges(self, self._embeds)
        except EmbedRefused as exc:
            self.status_message.emit(str(exc))
            return False
        return True

    def indent_selection(self, delta: int) -> None:
        """Add `delta` to the indent of every block the selection touches (one undo step).

        The blocks run from the one holding `selectionStart` to the one holding
        `selectionEnd`; a non-empty selection that ends exactly at a block start does not
        include that block. Each indent is clamped to `0..MAX_INDENT`.
        """
        document = self.document()
        cursor = self.textCursor()
        lo, hi = cursor.selectionStart(), cursor.selectionEnd()
        first = document.findBlock(lo)
        last = document.findBlock(hi)
        if hi > lo and hi == last.position():
            last = last.previous()
        changes = []
        block = first
        while block.isValid():
            before = block.blockFormat().indent()
            after = clamp_indent(before + delta)
            if after != before:
                changes.append((block.position(), after))
            if block == last:
                break
            block = block.next()
        if not changes:
            if delta > 0:
                self.status_message.emit(f"Already at the deepest indent ({MAX_INDENT}).")
            elif delta < 0:
                self.status_message.emit("Already at the left edge.")
            return
        edit = QTextCursor(document)
        edit.beginEditBlock()
        try:
            for position, indent in changes:
                edit.setPosition(position)
                block_format = edit.blockFormat()
                block_format.setIndent(indent)
                edit.setBlockFormat(block_format)
        finally:
            edit.endEditBlock()
        self._emit_tool_state()

    def insert_reference(self, rel: str) -> bool:
        """Insert `@rel` at the caret (replacing a selection); False when the syntax check refuses it."""
        syntax = write_paths.check_ref_syntax(rel)
        if not syntax.ok:
            self.error_message.emit(f"Cannot insert that reference: {syntax.reason}.")
            return False
        cursor = self.textCursor()
        before = cursor.block().text()[: cursor.positionInBlock()] if not cursor.hasSelection() else ""
        text = "@" + syntax.rel
        if before.endswith(":"):
            text = " " + text
        # An edit block keeps each insertion its own undo step: Qt would otherwise merge two adjacent
        # plain insertions into one command, exactly as it merges typed characters.
        cursor.beginEditBlock()
        try:
            cursor.insertText(text)
        finally:
            cursor.endEditBlock()
        self.setTextCursor(cursor)
        return True

    # -- keyboard and completer (hazard E) ---------------------------------------------------------

    def _popup_visible(self) -> bool:
        popup = self._completer.popup()
        return popup is not None and popup.isVisible()

    def _hide_completion(self) -> None:
        if self._popup_visible():
            self._completer.popup().hide()

    def _prefix_at_caret(self) -> str:
        cursor = self.textCursor()
        return keyword_prefix_before_cursor(cursor.block().text()[: cursor.positionInBlock()])

    def _refresh_completion(self) -> None:
        """Show, re-filter or hide the keyword popup for the text before the caret."""
        if self.textCursor().hasSelection() or self._preedit:
            self._hide_completion()
            return
        prefix = self._prefix_at_caret()
        if not matching_keywords(prefix):
            self._hide_completion()
            return
        popup = self._completer.popup()
        if prefix != self._completer.completionPrefix():
            self._completer.setCompletionPrefix(prefix)
            popup.setCurrentIndex(self._completer.completionModel().index(0, 0))
        rect = self.cursorRect()
        rect.setWidth(popup.sizeHintForColumn(0) + popup.verticalScrollBar().sizeHint().width())
        self._completer.complete(rect)

    def _insert_completion(self, name: str) -> None:
        """Replace the typed prefix with `Name: ` in ONE edit block (one undo step)."""
        if name not in INSTRUCTION_NAMES:
            return
        prefix = self._prefix_at_caret()
        if not prefix:
            return
        cursor = self.textCursor()
        cursor.beginEditBlock()
        try:
            cursor.movePosition(QTextCursor.MoveOperation.Left, QTextCursor.MoveMode.KeepAnchor, len(prefix))
            cursor.insertText(name + ": ")
        finally:
            cursor.endEditBlock()
        self.setTextCursor(cursor)

    def inputMethodEvent(self, event) -> None:
        """Track an IME preedit so the popup never fights a composition."""
        self._preedit = bool(event.preeditString())
        if self._preedit:
            self._hide_completion()
        super().inputMethodEvent(event)

    def keyPressEvent(self, event) -> None:
        """Completer keys, Tab / Shift+Tab indent, Shift+Enter = Enter, then Qt, then the popup."""
        key = event.key()
        modifiers = event.modifiers()
        popup_visible = self._popup_visible()
        if popup_visible and key in (
            Qt.Key.Key_Enter,
            Qt.Key.Key_Return,
            Qt.Key.Key_Escape,
            Qt.Key.Key_Tab,
            Qt.Key.Key_Backtab,
        ):
            event.ignore()  # the completer's own handler accepts (Tab / Enter) or closes (Escape) it
            return
        command = modifiers & (
            Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.AltModifier | Qt.KeyboardModifier.MetaModifier
        )
        shift = bool(modifiers & Qt.KeyboardModifier.ShiftModifier)
        if not command:
            if key == Qt.Key.Key_Backtab or (key == Qt.Key.Key_Tab and shift):
                self.indent_selection(-1)
                event.accept()
                return
            if key == Qt.Key.Key_Tab:
                self.indent_selection(1)
                event.accept()
                return
            if shift and key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                cursor = self.textCursor()
                cursor.insertBlock()
                self.setTextCursor(cursor)
                self.ensureCursorVisible()
                event.accept()
                return
        super().keyPressEvent(event)
        text = event.text()
        typed = bool(text) and text.isprintable() and not command
        if typed or key == Qt.Key.Key_Backspace or popup_visible:
            self._refresh_completion()

    # -- painting (hazards A, 2, 3) --------------------------------------------------------------------

    def paintEvent(self, event) -> None:
        """Qt paints the text, then the overlays (Dash, left lean) and the caret they covered."""
        super().paintEvent(event)
        painter = QPainter(self.viewport())
        try:
            self._paint_overlays(painter)
        except Exception as exc:  # noqa: BLE001 - a paint handler must never raise into Qt
            if not self._overlay_error_reported:
                self._overlay_error_reported = True
                print(f"WriteEditor: overlay painting failed: {type(exc).__name__}: {ascii(str(exc))}", file=sys.stderr)
        finally:
            painter.end()

    def _paint_overlays(self, painter: QPainter) -> None:
        palette = QPalette(self.palette())
        group = QPalette.ColorGroup.Active if self.isActiveWindow() else QPalette.ColorGroup.Inactive
        palette.setCurrentColorGroup(group)
        cursor = self.textCursor()
        selection = (cursor.selectionStart(), cursor.selectionEnd()) if cursor.hasSelection() else None
        offset = QPointF(-self.horizontalScrollBar().value(), -self.verticalScrollBar().value())
        clip = QRectF(self.viewport().rect())
        covers = paint_overlays(painter, self.document(), offset, clip, palette, selection)
        if covers and selection is None and self.hasFocus():
            # A lean cover erases the caret it sits on: put a solid one back (no blink inside a tilted run).
            rect = self.cursorRect()
            caret = QRectF(rect.x(), rect.y(), max(1, self.cursorWidth()), rect.height())
            if any(caret.intersects(cover) for cover in covers):
                painter.fillRect(caret, palette.color(QPalette.ColorRole.Text))
