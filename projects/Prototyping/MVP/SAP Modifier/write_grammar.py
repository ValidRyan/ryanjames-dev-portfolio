"""The instruction grammar for SAP Modifier: line classes, counts, scope tree, completion helpers.

Zero Qt dependency (`test_write_grammar.py` never imports PySide6). Consumed by
`write_resolve.py` (the scope forest is what the resolver walks),
`write_highlight.py` (spans for colouring) and `write_editor.py` (completion
helpers, status-bar counts). It has no idea what a path is; `write_paths.py`
owns that.

Line classes, decided from the line TEXT only (the indent comes from the
paragraph's int, never from whitespace, so a leading space does not indent):
- blank (empty or whitespace-only) -> ignored;
- `# ...` -> comment (must start with `#` in column 0) -> dimmed, ignored, counted;
- `Keyword: value` where Keyword is one of the eight `INSTRUCTION_NAMES`,
  ASCII case-insensitive, with the colon IMMEDIATELY after the keyword and the
  keyword at column 0 -> instruction ("clarity"). A value starting with `@` is a
  reference (`ref_text` = the rest, trimmed; empty means an "empty reference"),
  anything else - including an empty value - is a plain-text value;
- anything else non-blank (partial keyword, `Phrase : x`, prose) -> noise.

Documented choice: leading whitespace is NOT skipped (so `  Goals: x` is noise);
scope is what the Indent tool sets, and an accidental space showing up as
"Noise" in the status bar is honest feedback.

The scope tree (`parse_scope_tree`) takes `lines` as `(indent, text)` pairs
(what `WriteDocument.outline()` returns; bare strings are accepted as indent 0).
It is fully iterative - a 2000-deep input never recurses - and its dataclasses
opt out of the generated `__eq__`/`__repr__` (parent/children cycles and deep
chains would otherwise recurse).

The eight instructions are duds, as the plan says: `INSTRUCTIONS` maps each name
to a function returning `(None, "not implemented yet")`, the same shape as
`File Recompiler/instructions.py`'s placeholders (Copy/Paste/Cut). The message is
bare because the Run log supplies the `<Keyword> (line N, path):` prefix.

Explicitly out of scope: resolving references, reading files, Qt.
"""

from dataclasses import dataclass, field

# The octagon read clockwise from North (the order the eight are listed in).
INSTRUCTION_NAMES = (
    "Phrase",
    "Limits",
    "Meaning",
    "Goals",
    "Respond",
    "Understanding",
    "Info",
    "Knowledge",
)

# Decision D2: a text-valued instruction (`Goals: finish the draft`) may not
# carry indented children - "scope begins at an SI". Read at call time by
# `parse_scope_tree`, so tests (and a future user setting) can flip it.
TEXT_INSTRUCTIONS_CAN_NEST = False

_NOT_IMPLEMENTED = "not implemented yet"

_CANON = {name.lower(): name for name in INSTRUCTION_NAMES}


def _make_dud(name: str):
    def dud(text: str) -> tuple[str | None, str]:
        return None, _NOT_IMPLEMENTED

    dud.__name__ = f"{name.lower()}_instruction"
    dud.__doc__ = f"The `{name}:` instruction - an honest placeholder that does nothing yet."
    return dud


INSTRUCTIONS = {name: _make_dud(name) for name in INSTRUCTION_NAMES}


@dataclass(frozen=True)
class LineInfo:
    """Classification of one line's text.

    `kind` is "blank", "comment", "instruction" or "noise". For an instruction:
    `keyword` is the canonical name ("Phrase"), `value` the trimmed text after
    the colon, `is_ref` whether the value starts with `@`, and `ref_text` the
    trimmed path after the `@` ("" = empty reference; None when not a ref).
    Spans are `(start, end)` offsets into the ORIGINAL text: `kw_span` covers
    the keyword (not the colon); `value_span` the trimmed value including any
    `@` (None if the value is empty); `ref_span` the path after the `@` (an
    empty span for an empty reference; None when not a ref). Non-instructions
    have `keyword`/`value`/spans None (value "" ).
    """

    kind: str
    keyword: str | None = None
    value: str = ""
    is_ref: bool = False
    ref_text: str | None = None
    kw_span: tuple[int, int] | None = None
    value_span: tuple[int, int] | None = None
    ref_span: tuple[int, int] | None = None


_BLANK = LineInfo("blank")
_COMMENT = LineInfo("comment")
_NOISE = LineInfo("noise")


def classify_line(text: str) -> LineInfo:
    """Classify a line of text per the module docstring's rules."""
    if not text.strip():
        return _BLANK
    if text.startswith("#"):
        return _COMMENT
    colon = text.find(":")
    if colon <= 0:
        return _NOISE
    word = text[:colon]
    # ASCII-only case folding: `str.lower()` would map the Kelvin sign U+212A
    # to "k" and accept a look-alike of "Knowledge".
    canon = _CANON.get(word.lower()) if word.isascii() else None
    if canon is None:
        return _NOISE
    rest = text[colon + 1 :]
    lead = len(rest) - len(rest.lstrip())
    value = rest.strip()
    start = colon + 1 + lead
    value_span = (start, start + len(value)) if value else None
    is_ref = value.startswith("@")
    ref_text = None
    ref_span = None
    if is_ref:
        after = value[1:]
        ref_lead = len(after) - len(after.lstrip())
        ref_text = after.strip()
        ref_start = start + 1 + ref_lead
        ref_span = (ref_start, ref_start + len(ref_text)) if ref_text else (start + 1, start + 1)
    return LineInfo("instruction", canon, value, is_ref, ref_text, (0, colon), value_span, ref_span)


@dataclass(frozen=True)
class LineCounts:
    """How many lines of each class: `clarity` (instructions), `noise`, `comment`, `blank`."""

    clarity: int = 0
    noise: int = 0
    comment: int = 0
    blank: int = 0


def _pair(item) -> tuple[int, str]:
    if isinstance(item, str):
        return 0, item
    indent, text = item
    return int(indent), text


def count_lines(lines) -> LineCounts:
    """Count line classes over `lines` (`(indent, text)` pairs or bare strings)."""
    clarity = noise = comment = blank = 0
    for item in lines:
        kind = classify_line(_pair(item)[1]).kind
        if kind == "instruction":
            clarity += 1
        elif kind == "noise":
            noise += 1
        elif kind == "comment":
            comment += 1
        else:
            blank += 1
    return LineCounts(clarity, noise, comment, blank)


@dataclass(eq=False, repr=False)
class ScopeNode:
    """One instruction line in the scope tree.

    `line` is the 1-based position in the input (blank and comment lines count),
    `indent` the paragraph's indent int, `info` the `LineInfo`. `parent` is the
    node this one is listed under (None for a root). `structural` is None, or
    "under_noise" / "under_text" when the grammar already knows this node is in
    an illegal place (the resolver adds "under_operator" later). `children`
    holds the AUTHORED (indent-nested) instruction lines in order.
    """

    line: int
    indent: int
    info: LineInfo
    parent: "ScopeNode | None" = None
    structural: str | None = None
    children: list = field(default_factory=list)

    @property
    def keyword(self) -> str:
        return self.info.keyword or ""

    def __repr__(self) -> str:
        return f"ScopeNode(line={self.line}, indent={self.indent}, keyword={self.info.keyword!r})"


@dataclass(frozen=True)
class Diagnostic:
    """A grammar warning. `code` is "first_line_indented", "indent_jump", ...

    `line` is the line the message is attached to (shown next to it);
    `subject_line` is the line that caused it. For `indent_jump` the message
    attaches to the nearest shallower line (the parent); for `first_line_indented`
    to the line itself.
    """

    code: str
    line: int
    subject_line: int
    message: str


@dataclass
class ScopeForest:
    """`roots` (top-level `ScopeNode`s), `diagnostics` and the `counts` of the input."""

    roots: list
    diagnostics: list
    counts: LineCounts


@dataclass
class _Frame:
    """A stack entry. `node` is None for a noise line (never a tree node);
    `anchor` is the nearest instruction node at or above this entry; `line`
    is the 1-based line number the entry came from."""

    indent: int
    node: "ScopeNode | None"
    anchor: "ScopeNode | None"
    line: int


def parse_scope_tree(lines) -> ScopeForest:
    """Build the scope forest from `lines` (`(indent, text)` pairs or bare strings).

    Iterative, single pass over an explicit stack. Blank and comment lines are
    transparent (they neither open nor close a scope). For each instruction or
    noise line the stack is popped while `top.indent >= indent`, so an outdent
    to a level that was never seen simply lands on the nearest shallower entry.
    The parent is the stack top. Jumps of more than one level (0 -> 3) are
    legal but produce an `indent_jump` diagnostic attached to the parent; a
    first participating line with indent > 0 produces `first_line_indented`.

    Noise lines are pushed (so what is nested under them is recognised) but are
    never nodes: an instruction whose parent is noise is listed under the
    nearest instruction ancestor (or as a root) and flagged
    `structural="under_noise"`. An instruction under a text-valued instruction
    is flagged `"under_text"` unless `TEXT_INSTRUCTIONS_CAN_NEST` is True.
    Children under a reference are always allowed here - whether the target can
    hold them (an operator cannot) is the resolver's decision.
    """
    roots: list = []
    diagnostics: list = []
    stack: list[_Frame] = []
    clarity = noise = comment = blank = 0
    first_seen = False
    for number, item in enumerate(lines, start=1):
        indent, text = _pair(item)
        info = classify_line(text)
        if info.kind == "blank":
            blank += 1
            continue
        if info.kind == "comment":
            comment += 1
            continue
        is_instruction = info.kind == "instruction"
        if is_instruction:
            clarity += 1
        else:
            noise += 1
        if not first_seen:
            first_seen = True
            if indent > 0:
                diagnostics.append(
                    Diagnostic("first_line_indented", number, number, "the first line is indented; it is treated as top level")
                )
        while stack and stack[-1].indent >= indent:
            stack.pop()
        parent_frame = stack[-1] if stack else None
        if parent_frame is not None and indent - parent_frame.indent > 1:
            diagnostics.append(
                Diagnostic(
                    "indent_jump",
                    parent_frame.line,
                    number,
                    f"line {number} jumps from indent {parent_frame.indent} to {indent}",
                )
            )
        if not is_instruction:
            anchor = parent_frame.anchor if parent_frame is not None else None
            stack.append(_Frame(indent, None, anchor, number))
            continue
        structural = None
        if parent_frame is not None:
            if parent_frame.node is None:
                structural = "under_noise"
            elif not parent_frame.node.info.is_ref and not TEXT_INSTRUCTIONS_CAN_NEST:
                structural = "under_text"
        # Where the node is listed: under its real parent, except that a noise
        # parent is never a tree node, so fall back to the nearest instruction.
        tree_parent = None
        if parent_frame is not None:
            tree_parent = parent_frame.node if parent_frame.node is not None else parent_frame.anchor
        node = ScopeNode(number, indent, info, tree_parent, structural)
        if tree_parent is None:
            roots.append(node)
        else:
            tree_parent.children.append(node)
        stack.append(_Frame(indent, node, node, number))
    return ScopeForest(roots, diagnostics, LineCounts(clarity, noise, comment, blank))


def keyword_prefix_before_cursor(text_before_cursor: str) -> str:
    """Return the partial keyword being typed at the start of a line, else "".

    `text_before_cursor` is the block's text from its start up to the caret. A
    prefix exists only when that whole span is 1 or more ASCII letters (so
    typing the colon, a space, or anything else ends completion). Truthiness
    is the intended test: "" means "no completion".
    """
    if text_before_cursor and text_before_cursor.isascii() and text_before_cursor.isalpha():
        return text_before_cursor
    return ""


def matching_keywords(prefix: str) -> list[str]:
    """The instruction names starting with `prefix` (ASCII case-insensitive), in octagon order.

    An empty prefix matches nothing (no popup for an empty line).
    """
    if not prefix or not prefix.isascii():
        return []
    lowered = prefix.lower()
    return [name for name in INSTRUCTION_NAMES if name.lower().startswith(lowered)]
