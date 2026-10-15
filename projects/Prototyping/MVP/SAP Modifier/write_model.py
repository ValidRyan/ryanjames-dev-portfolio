"""Pure data model for SAP Modifier documents: runs, embeds, paragraphs, tag algebra.

Zero Qt dependency, by design - the same discipline `Folder Replication/store_model.py`
keeps: plain dataclasses and functions that `test_write_model.py` exercises
without importing PySide6. `write_format.py` (also Qt-free) owns the JSON file
format on top of these types; `write_docmodel.py` (Qt) converts between these
types and a `QTextDocument`. Nothing in this module touches the filesystem.

A `WriteDocument` is a list of `Paragraph`s; a paragraph has an `indent` (an
int - the scope depth of the instruction language, NEVER derived from
whitespace) and a list of runs. A run is either a text `Run` (text plus the
canonical formatting `tags`) or an `EmbedRun` (a PNG snapshot plus the original
text, `source`, kept underneath so Un-embed can restore it). The tree, counts
and Run all derive from the logical text (`outline()`), never from the PNG.

The document-level caps (`MAX_PARAGRAPHS`, embed limits, ...) live here so the
loader, the editor and the embed pipeline share one definition;
`write_format.py` re-exports them for convenience.

Canonical tags: `bold` / `line` / `dash` (True when on, absent when off),
`italic` ("right" or "left"; absent = upright) and `spread` (absolute
letter-spacing in px, clamped to `SPREAD_MIN..SPREAD_MAX`, absent = 0).

Explicitly out of scope: file I/O, Qt types, and any parsing of the
instruction grammar (`write_grammar.py`).
"""

import hashlib
import json
import math
import unicodedata
from dataclasses import dataclass, field

FORMAT_VERSION = 1
MAX_INDENT = 32
SPREAD_MIN = -3.0
SPREAD_MAX = 32.0
SPREAD_STEP = 1.0

MAX_PARAGRAPHS = 20_000
MAX_RUNS_PER_PARAGRAPH = 2_000
MAX_TOTAL_RUNS = 200_000
MAX_RUN_TEXT = 100_000

MAX_EMBEDS = 200
MAX_EMBED_PNG_BYTES = 2 * 1024 * 1024
MAX_TOTAL_EMBED_BYTES = 16 * 1024 * 1024
MAX_IMAGE_DIM = 4096
MAX_IMAGE_PIXELS = 4_000_000

TAG_KEYS = ("bold", "line", "dash", "italic", "spread")
ITALIC_STATES = ("right", "left")


@dataclass
class Run:
    """A stretch of text with one set of canonical `tags` (see the module docstring)."""

    text: str
    tags: dict = field(default_factory=dict)


@dataclass
class EmbedRun:
    """An inline PNG snapshot plus the original text it was made from.

    `source` holds text `Run`s only (never an embed - no nesting) and is a
    single paragraph's worth of runs. The PNG is display-only; everything that
    reads the document (tree, counts, Run, plain text) uses `source`.
    """

    png: bytes
    source: list = field(default_factory=list)


@dataclass
class Paragraph:
    """One line of the document: an `indent` int and its runs (Run | EmbedRun)."""

    indent: int = 0
    runs: list = field(default_factory=list)


def runs_text(runs) -> str:
    """Concatenate the logical text of runs (an embed contributes its `source` text)."""
    parts = []
    for run in runs:
        if isinstance(run, EmbedRun):
            parts.append("".join(r.text for r in run.source))
        else:
            parts.append(run.text)
    return "".join(parts)


def paragraph_text(paragraph: Paragraph) -> str:
    """The logical text of one paragraph (embeds replaced by their source text)."""
    return runs_text(paragraph.runs)


@dataclass
class WriteDocument:
    """The whole document. `paragraphs` always holds at least one paragraph.

    Documented choice: an empty list passed in is repaired to one empty
    paragraph rather than rejected, so a `WriteDocument` is never "no lines" -
    matching what an editor with an empty document actually contains.
    """

    paragraphs: list = field(default_factory=lambda: [Paragraph()])
    version: int = FORMAT_VERSION

    def __post_init__(self):
        if not self.paragraphs:
            self.paragraphs = [Paragraph()]

    def outline(self) -> list[tuple[int, str]]:
        """Return `[(indent, logical_text), ...]`, one entry per paragraph.

        This is the only view the grammar/resolver/Run see: indent ints come
        from the paragraphs, text from the runs (embeds -> their `source`).
        """
        return [(p.indent, paragraph_text(p)) for p in self.paragraphs]

    def to_plain_text(self, indent_width: int = 0) -> str:
        """Paragraph texts joined by newlines.

        Documented choice: no indentation whitespace by default (so the result
        matches the editor's `toPlainText()` modulo embeds); pass
        `indent_width=N` to prefix N spaces per indent level for a
        human-readable export. Display-only - never re-parsed.
        """
        pad = " " * max(0, indent_width)
        return "\n".join(pad * p.indent + paragraph_text(p) for p in self.paragraphs)


def normalize_tags(tags) -> dict:
    """Return the canonical form of `tags`: known keys only, off-values dropped.

    bold/line/dash are kept only when truthy (as True); italic only when
    "right" or "left"; spread only when a finite, non-bool number that is not
    zero after clamping to the range and rounding to 2 dp. Unknown keys are
    ignored (forward compatibility) and keys come out in `TAG_KEYS` order so
    serialised files are deterministic. Never raises on odd input.
    """
    out: dict = {}
    if not isinstance(tags, dict):
        return out
    for key in ("bold", "line", "dash"):
        if tags.get(key) is True:
            out[key] = True
    italic = tags.get("italic")
    if isinstance(italic, str) and italic in ITALIC_STATES:
        out["italic"] = italic
    spread = tags.get("spread")
    if isinstance(spread, (int, float)) and not isinstance(spread, bool) and math.isfinite(spread):
        value = _clamp_spread(float(spread))
        if value != 0.0:
            out["spread"] = value
    return out


def coalesce_runs(runs) -> list:
    """Merge adjacent text runs with equal canonical tags; drop empty text runs.

    Never merges across an `EmbedRun` (embeds pass through unchanged, and a
    text run on either side stays separate). Returns new `Run` objects for text
    and does not mutate the input. Documented choice: a merged run is capped at
    `MAX_RUN_TEXT` characters (a new run starts after that) so a saved file
    never contains a run the strict loader would reject.
    """
    out: list = []
    for run in runs:
        if isinstance(run, EmbedRun):
            out.append(run)
            continue
        if not run.text:
            continue
        tags = normalize_tags(run.tags)
        prev = out[-1] if out else None
        if isinstance(prev, Run) and prev.tags == tags and len(prev.text) + len(run.text) <= MAX_RUN_TEXT:
            prev.text += run.text
        else:
            out.append(Run(run.text, tags))
    return out


_NEWLINE_LIKE = frozenset("\r\n\v\f\N{LINE SEPARATOR}\N{PARAGRAPH SEPARATOR}\u0085")


def sanitize_text(text: str, *, keep_newlines: bool = False) -> str:
    """Scrub text so it is safe to store as run text or to insert as pasted text.

    Removes U+FFFC (the object-replacement char an embed shows as) and every C0/C1
    control character except tab; turns lone surrogates into U+FFFD (they cannot
    be encoded as UTF-8). Newline-like characters (LF, CR, VT, FF, NEL, U+2028,
    U+2029) are handled per `keep_newlines`:

    - False (default - the run-text rule): each newline-like character (a CRLF
      pair counts once) becomes one space, because a run lives inside a single
      paragraph. `document_to_model` and the file loader use this.
    - True (the paste rule): every newline-like character becomes "\\n" (CRLF ->
      one "\\n") so a pasted block still splits into paragraphs.

    Callers detect a change with `sanitize_text(t) != t`.
    """
    out = []
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        i += 1
        if ch in _NEWLINE_LIKE:
            if ch == "\r" and i < n and text[i] == "\n":
                i += 1
            out.append("\n" if keep_newlines else " ")
            continue
        if ch == "\N{OBJECT REPLACEMENT CHARACTER}":
            continue
        cat = unicodedata.category(ch)
        if cat == "Cc" and ch != "\t":
            continue
        if cat == "Cs":
            out.append("\N{REPLACEMENT CHARACTER}")
            continue
        out.append(ch)
    return "".join(out)


def next_italic(state: str | None) -> str | None:
    """Advance the Italics cycle: None (upright) -> "right" -> "left" -> None."""
    if state == "right":
        return "left"
    if state == "left":
        return None
    return "right"


def toggle_target(states) -> bool:
    """The value a bold/line/dash toggle should apply to a selection.

    `states` is one bool per selected text fragment. All on -> turn OFF (False);
    any off (or nothing selected) -> turn ON (True): "apply to all if any lacks
    it; remove only if all have it".
    """
    states = list(states)
    if states and all(states):
        return False
    return True


def _clamp_spread(value: float) -> float:
    return round(min(SPREAD_MAX, max(SPREAD_MIN, value)), 2) + 0.0


def apply_spread(current: float | None, delta: float) -> float:
    """Add `delta` px to a letter-spacing value, clamp to the range, round to 2 dp."""
    return _clamp_spread(float(current or 0.0) + float(delta))


def clamp_indent(indent: int) -> int:
    """Clamp an indent level into `0..MAX_INDENT`."""
    return max(0, min(MAX_INDENT, int(indent)))


class EmbedRegistry:
    """Session store for embed PNGs and their source text, keyed by a content id.

    The id is the SHA-1 of the PNG bytes plus the canonical source runs, so two
    embeds with the same picture but different underlying text (a spoofed
    file) can never share an id. `add()` dedupes identical content and
    enforces `MAX_EMBEDS` / `MAX_TOTAL_EMBED_BYTES` (`ValueError`). The
    registry is append-only for a session (only `clear()` empties it) so an
    undo that resurrects an embed always finds its picture.
    """

    def __init__(self, max_embeds: int = MAX_EMBEDS, max_total_bytes: int = MAX_TOTAL_EMBED_BYTES):
        self._max_embeds = max_embeds
        self._max_total_bytes = max_total_bytes
        self._items: dict[str, EmbedRun] = {}
        self._total_bytes = 0

    @staticmethod
    def _copy_source(source) -> list:
        return [Run(r.text, normalize_tags(r.tags)) for r in source]

    def add(self, png: bytes, source) -> str:
        """Store an embed and return its id; identical content returns the same id."""
        src = self._copy_source(source)
        digest = hashlib.sha1(usedforsecurity=False)
        digest.update(png)
        digest.update(b"\0")
        digest.update(json.dumps([[r.text, r.tags] for r in src], sort_keys=True, ensure_ascii=True).encode("ascii"))
        embed_id = digest.hexdigest()
        if embed_id in self._items:
            return embed_id
        if len(self._items) >= self._max_embeds:
            raise ValueError(f"too many embeds (limit {self._max_embeds})")
        if self._total_bytes + len(png) > self._max_total_bytes:
            raise ValueError(f"embed pictures exceed the {self._max_total_bytes}-byte budget")
        self._items[embed_id] = EmbedRun(bytes(png), src)
        self._total_bytes += len(png)
        return embed_id

    def get(self, embed_id: str) -> EmbedRun | None:
        """Return a copy of the stored embed (png + source), or None for an unknown id."""
        item = self._items.get(embed_id)
        if item is None:
            return None
        return EmbedRun(item.png, self._copy_source(item.source))

    def clear(self) -> None:
        """Forget every embed (used when a different document replaces the session)."""
        self._items.clear()
        self._total_bytes = 0

    @property
    def total_bytes(self) -> int:
        """Sum of the stored PNG sizes."""
        return self._total_bytes

    def __len__(self) -> int:
        return len(self._items)

    def __contains__(self, embed_id: str) -> bool:
        return embed_id in self._items
