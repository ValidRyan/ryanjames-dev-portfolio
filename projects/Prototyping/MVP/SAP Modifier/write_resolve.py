"""Iterative, valve-limited resolver for SAP Modifier reference trees.

Zero Qt dependency (`test_write_resolve.py` never imports PySide6). Takes the
root Write's outline plus the root file, and produces the read-only resolved
tree the tree pane shows and the Run command walks. Built on
`write_paths.py` (every reference goes through `resolve_ref`), `write_grammar.py`
(the scope forest), `write_format.py` (nested `.mwrite` loading) and
`write_model.py` (sanitiser); it imports nothing from other MVPs.

What a node can be (`kind`): `text` (a plain-text instruction), `write`,
`store`, `doc`, `prog`, `operator` (any other existing file - NEVER opened; the
chain ends), `folder` (an ordinary directory - an opaque leaf), `content` (a raw
line of a `.mdoc`/`.mprog`) or `marker` (`[unreadable]`). Exactly one `state`:
`ok`, `broken`, `blocked`, `cycle`, `truncated`, `unsaved`, `structural`.
An SI node's children are the lines AUTHORED under it (`role="authored"`)
followed by the target's own contents (`role="resolved"`): a nested Write's
instruction lines, a Store's directory listing, or a Doc/Prog's raw lines.
Nothing is copied - it is all re-read on each pass ("live").

Safety design:
- The resolver is one explicit-stack loop (no recursion), so a 2000-deep chain
  or a hostile fan-out cannot exhaust Python's stack; the tests run it under a
  tiny recursion limit.
- Valves (`Limits`): `MAX_DEPTH` 64 levels, `MAX_NODES` 2000 nodes,
  `MAX_READ_BYTES` per raw Doc/Prog read, `MAX_WRITE_BYTES` per nested Write,
  `MAX_TOTAL_READ_BYTES` per pass, `MAX_LISTING_SCAN` directory entries,
  `MAX_CONTENT_LINES` / `MAX_CONTENT_LINE_CHARS` per raw file. A tripped valve
  marks the PARENT node `truncated` with the reason; it never raises.
- Cycles: detected by REAL path (so a junction that aliases a Write is caught)
  along the current ANCESTOR chain only. The same file reached twice by two
  different routes (a diamond) is legal and expands both times.
- A per-pass cache (each file read/parsed once, each listing scanned once, each
  `(reference, folder)` resolved once) is what makes a fan-out bomb finish
  quickly; it lives for one `resolve_tree` call only, so the next pass is live.
- Operators and ordinary folders are never opened; Doc/Prog files are read as
  binary, capped, decoded as strict UTF-8 and never parsed (an `Info: @x`
  inside one is inert text).
- I/O failures never raise out of `resolve_tree`; they become a node state.

Documented choices: the node `kind` for a reference that does not resolve
(`broken`/`blocked`/`unsaved`) is a best guess from the name (`.mwrite` ->
`write`, anything else -> `operator`), and authored children under such a
node are allowed (its kind is unknown), unlike under a real operator where
they are `structural`. A `trail` is the chain of containing Writes joined by
" > " (`main.mwrite > sub/a.mwrite`); a file inside a Store extends it with
`store.mstore/file`. `iter_nodes` and `run_tree` accept either a
`ResolveResult` or a `ResolvedNode`.

Explicitly out of scope: executing anything (Run is the honest "not
implemented yet" log), Qt, and writing files.
"""

import codecs
import os
import unicodedata
from dataclasses import dataclass, field
from itertools import islice

from write_format import WriteFileTooLarge, WriteFormatError, parse_write_bytes, read_capped
from write_grammar import INSTRUCTIONS, LineCounts, parse_scope_tree
from write_model import sanitize_text
from write_paths import chain_key, real_root, resolve_ref, si_kind_for_name

MAX_DEPTH = 64
MAX_NODES = 2000
MAX_READ_BYTES = 1 * 1024 * 1024
MAX_WRITE_BYTES = 8 * 1024 * 1024
MAX_TOTAL_READ_BYTES = 32 * 1024 * 1024
MAX_LISTING_SCAN = 5000
MAX_CONTENT_LINES = 200
MAX_CONTENT_LINE_CHARS = 200

_STRUCTURAL_REASONS = {
    "under_noise": "indented under a line that is not an instruction",
    "under_text": "a text instruction cannot hold indented lines",
    "under_operator": "an operator is a leaf: the chain ends here",
}
_IGNORED_SCOPE = "inside a scope that is ignored"


@dataclass(frozen=True)
class Limits:
    """The safety valves, one field per module constant (override in tests)."""

    max_depth: int = MAX_DEPTH
    max_nodes: int = MAX_NODES
    max_read_bytes: int = MAX_READ_BYTES
    max_write_bytes: int = MAX_WRITE_BYTES
    max_total_read_bytes: int = MAX_TOTAL_READ_BYTES
    max_listing_scan: int = MAX_LISTING_SCAN
    max_content_lines: int = MAX_CONTENT_LINES
    max_content_line_chars: int = MAX_CONTENT_LINE_CHARS


@dataclass(eq=False)
class ResolvedNode:
    """One row of the resolved tree.

    `role`: "root" (the root Write), "authored" (written under its parent in
    the same Write) or "resolved" (came from resolving the parent's target).
    `label` is display text; `keyword`/`value`/`line` describe an instruction
    line (`keyword` None for store entries, content lines and markers; `line` is
    the 1-based line in its Write, or the line of a raw file, or 0); `trail` the
    chain of containing Writes; `kind` / `state` / `reason` as in the module
    docstring; `ref` the `@path` text for a reference line, else None;
    `real_path` the verified real path when resolved; `children` in display order.
    """

    role: str
    label: str
    keyword: str | None = None
    value: str = ""
    line: int = 0
    trail: str = ""
    kind: str = "text"
    state: str = "ok"
    reason: str = ""
    ref: str | None = None
    real_path: str | None = None
    children: list = field(default_factory=list, repr=False)

    def __repr__(self) -> str:
        return f"ResolvedNode({self.role}, {self.label!r}, kind={self.kind}, state={self.state})"


@dataclass(eq=False)
class ResolveResult:
    """`root` node, `nodes` (count of nodes below the root), whether any valve
    tripped (`truncated`), `warnings`, and the root Write's own grammar
    `diagnostics` and line `counts` (what the status bar shows)."""

    root: ResolvedNode
    nodes: int
    truncated: bool
    warnings: list
    diagnostics: list
    counts: LineCounts


@dataclass(frozen=True)
class _Ctx:
    """Where a forest's lines live: the Write's real folder, the ancestor chain
    (normcased real paths of Writes/Stores currently open above), and the trail."""

    base_dir: str | None
    chain: tuple
    trail: str


def _display(name: str) -> str:
    """Make a file name safe to show: control/format characters become '?'."""
    return "".join("?" if unicodedata.category(c) in ("Cc", "Cf", "Cs") else c for c in name)[:255]


def _guess_kind(ref_text: str) -> str:
    """Best-guess kind for a reference that did not resolve (see module docstring)."""
    last = ref_text.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
    return si_kind_for_name(last, False) or si_kind_for_name(last, True) or "operator"


class _Resolver:
    """One resolution pass: valves, caches and the explicit task stack."""

    def __init__(self, limits: Limits, root_real: str | None):
        self.limits = limits
        self.root_real = root_real
        self.nodes = 0
        self.truncated = False
        self.warnings: list[str] = []
        self._warned: set[str] = set()
        self.total_read = 0
        self._dropped: dict[int, list] = {}
        self._writes: dict = {}
        self._texts: dict = {}
        self._listings: dict = {}
        self._refs: dict = {}

    # -- valves ---------------------------------------------------------

    def _warn_once(self, message: str) -> None:
        if message not in self._warned:
            self._warned.add(message)
            self.warnings.append(message)

    def _trip(self, parent: ResolvedNode, what: str, remaining: int) -> None:
        self.truncated = True
        self._warn_once(f"{what} reached: some entries are not shown")
        entry = self._dropped.get(id(parent))
        if entry is None:
            self._dropped[id(parent)] = [parent, what, remaining]
            if parent.state == "ok":
                parent.state = "truncated"
        else:
            entry[2] += remaining

    def _room(self, parent: ResolvedNode, depth: int, remaining: int) -> bool:
        """Can `remaining` more children be attached under `parent` at `depth`?
        If not, mark the parent truncated (once) and count what was dropped."""
        if depth > self.limits.max_depth:
            self._trip(parent, f"MAX_DEPTH {self.limits.max_depth}", remaining)
            return False
        if self.nodes >= self.limits.max_nodes:
            self._trip(parent, f"MAX_NODES {self.limits.max_nodes}", remaining)
            return False
        return True

    def finish(self) -> None:
        for parent, what, count in self._dropped.values():
            text = f"{what}; {count} not shown"
            if parent.state == "truncated":
                parent.reason = f"{parent.reason}; {text}" if parent.reason else text

    def _append(self, parent: ResolvedNode, node: ResolvedNode) -> None:
        parent.children.append(node)
        self.nodes += 1

    # -- caches ---------------------------------------------------------

    def _resolve(self, raw: str, base_dir: str | None):
        key = (raw, base_dir)
        res = self._refs.get(key)
        if res is None:
            res = resolve_ref(raw, base_dir, self.root_real)
            self._refs[key] = res
        return res

    def _load_write(self, path: str, rel: str):
        """Read + parse a nested Write once per pass -> ("ok", forest) or (state, reason)."""
        limits = self.limits
        if self.total_read >= limits.max_total_read_bytes:
            self.truncated = True
            self._warn_once("MAX_TOTAL_READ_BYTES reached: some files were not read")
            return "truncated", f"read budget MAX_TOTAL_READ_BYTES ({limits.max_total_read_bytes}) is used up"
        try:
            data = read_capped(path, limits.max_write_bytes)
        except WriteFileTooLarge:
            self.total_read += limits.max_write_bytes + 1
            self.truncated = True
            return "truncated", f"Write is larger than MAX_WRITE_BYTES ({limits.max_write_bytes})"
        except OSError as exc:
            return "broken", f"cannot read: {exc.strerror or exc}"
        self.total_read += len(data)
        try:
            result = parse_write_bytes(data, limits.max_write_bytes)
        except WriteFormatError as exc:
            return "broken", f"invalid Write: {exc.message} (at {exc.where})"
        for warning in result.warnings:
            self.warnings.append(f"{rel}: {warning}")
        return "ok", parse_scope_tree(result.document.outline())

    def _load_text(self, path: str):
        """Read a raw Doc/Prog once per pass -> (status, lines, note).

        status "ok" (lines, maybe a `note` when capped), "unreadable" (note says
        why) or "truncated" (budget used up, no lines)."""
        limits = self.limits
        if self.total_read >= limits.max_total_read_bytes:
            self.truncated = True
            self._warn_once("MAX_TOTAL_READ_BYTES reached: some files were not read")
            return "truncated", [], f"read budget MAX_TOTAL_READ_BYTES ({limits.max_total_read_bytes}) is used up"
        try:
            with open(path, "rb") as handle:
                data = handle.read(limits.max_read_bytes + 1)
        except OSError as exc:
            return "unreadable", [], f"cannot read: {exc.strerror or exc}"
        self.total_read += len(data)
        cut_bytes = len(data) > limits.max_read_bytes
        if cut_bytes:
            data = data[: limits.max_read_bytes]
        decoder = codecs.getincrementaldecoder("utf-8")("strict")
        try:
            text = decoder.decode(data, final=not cut_bytes)
        except UnicodeDecodeError:
            return "unreadable", [], "not valid UTF-8 text"
        if "\x00" in text:
            return "unreadable", [], "binary data (contains a NUL byte)"
        cap = limits.max_content_lines
        pieces = text.split("\n", cap)
        cut_lines = False
        if len(pieces) > cap:
            cut_lines = bool(pieces[cap])
            pieces = pieces[:cap]
        elif pieces and pieces[-1] == "":
            pieces.pop()
        lines = [sanitize_text(p.rstrip("\r")[: limits.max_content_line_chars]) for p in pieces]
        note = ""
        if cut_bytes:
            note = f"file is larger than MAX_READ_BYTES ({limits.max_read_bytes}); showing the start"
        elif cut_lines:
            note = f"showing only the first {cap} lines"
        return "ok", lines, note

    def _scan_store(self, path: str):
        """List a Store directory once per pass -> (names, capped) or (None, reason)."""
        cap = self.limits.max_listing_scan
        try:
            with os.scandir(path) as entries:
                names = [entry.name for entry in islice(entries, cap + 1)]
        except OSError as exc:
            return None, f"cannot list: {exc.strerror or exc}"
        capped = len(names) > cap
        names = names[:cap]
        names.sort(key=lambda n: (n.casefold(), n))
        return names, capped

    # -- node construction ---------------------------------------------

    def _line_node(self, scope, ctx: _Ctx, depth: int, role: str, inert: str | None) -> tuple:
        """Build the node for one instruction line; return `(node, follow_tasks)`.

        `follow_tasks` are in execution order (authored children first, then
        the target's resolved contents)."""
        info = scope.info
        label = f"{info.keyword}: {info.value}" if info.value else f"{info.keyword}:"
        node = ResolvedNode(
            role, label, info.keyword, info.value, scope.line, ctx.trail,
            "text", "ok", "", info.ref_text if info.is_ref else None,
        )
        if info.is_ref:
            node.kind = _guess_kind(info.ref_text)
        authored = scope.children
        if inert is not None or scope.structural is not None:
            node.state = "structural"
            node.reason = inert if inert is not None else _STRUCTURAL_REASONS[scope.structural]
            follow = [("forest", node, authored, ctx, depth + 1, "authored", _IGNORED_SCOPE)] if authored else []
            return node, follow
        if not info.is_ref:
            follow = [("forest", node, authored, ctx, depth + 1, "authored", None)] if authored else []
            return node, follow
        res = self._resolve(info.ref_text, ctx.base_dir)
        if res.state != "ok":
            node.state = res.state
            node.reason = res.reason
            follow = [("forest", node, authored, ctx, depth + 1, "authored", None)] if authored else []
            return node, follow
        return node, self._open_target(node, res, res.rel, ctx, depth, authored)

    def _open_target(self, node: ResolvedNode, res, rel: str, ctx: _Ctx, depth: int, authored) -> list:
        """Decide what an OK resolution means for `node`; return follow tasks."""
        kind = si_kind_for_name(os.path.basename(res.path), res.is_dir) or ("folder" if res.is_dir else "operator")
        node.kind = kind
        node.real_path = res.path
        follow: list = []
        if kind in ("operator", "folder"):
            if authored:
                follow.append(("forest", node, authored, ctx, depth + 1, "authored", _STRUCTURAL_REASONS["under_operator"]))
            return follow
        key = chain_key(res.path)
        if key in ctx.chain:
            node.state = "cycle"
            node.reason = f"{rel} is already open above it (reference cycle)"
            if authored:
                follow.append(("forest", node, authored, ctx, depth + 1, "authored", None))
            return follow
        if authored:
            follow.append(("forest", node, authored, ctx, depth + 1, "authored", None))
        follow.append(("expand", node, res.path, kind, key, rel, ctx, depth + 1))
        return follow

    # -- task handlers --------------------------------------------------

    def run_forest(self, parent, scopes, ctx, depth, role, inert, stack) -> None:
        groups = []
        for i, scope in enumerate(scopes):
            if not self._room(parent, depth, len(scopes) - i):
                break
            node, follow = self._line_node(scope, ctx, depth, role, inert)
            self._append(parent, node)
            groups.append(follow)
        _push(stack, groups)

    def run_expand(self, node, path, kind, key, rel, ctx, depth, stack) -> None:
        if not self._room(node, depth, 1):
            return
        if kind == "write":
            entry = self._writes.get(key)
            if entry is None:
                entry = self._load_write(path, rel)
                self._writes[key] = entry
            state, payload = entry
            if state != "ok":
                node.state = state
                node.reason = payload
                return
            child = _Ctx(os.path.dirname(path), ctx.chain + (key,), f"{ctx.trail} > {rel}")
            stack.append(("forest", node, payload.roots, child, depth, "resolved", None))
        elif kind == "store":
            listing = self._listings.get(key)
            if listing is None:
                listing = self._scan_store(path)
                self._listings[key] = listing
            names, extra = listing
            if names is None:
                node.state = "broken"
                node.reason = extra
                return
            if extra:
                node.state = "truncated"
                node.reason = f"listing capped at MAX_LISTING_SCAN ({self.limits.max_listing_scan})"
                self.truncated = True
                self._warn_once(f"MAX_LISTING_SCAN {self.limits.max_listing_scan} reached: the listing is incomplete")
            child = _Ctx(path, ctx.chain + (key,), ctx.trail)
            stack.append(("entries", node, names, path, child, depth, rel))
        else:
            entry = self._texts.get(key)
            if entry is None:
                entry = self._load_text(path)
                self._texts[key] = entry
            status, lines, note = entry
            if status == "truncated":
                node.state = "truncated"
                node.reason = note
                return
            if status == "unreadable":
                marker = ResolvedNode("resolved", "[unreadable]", None, "", 0, ctx.trail, "marker", "broken", note)
                if self._room(node, depth, 1):
                    self._append(node, marker)
                return
            if note:
                node.state = "truncated"
                node.reason = note
                self.truncated = True
            for i, text in enumerate(lines):
                if not self._room(node, depth, len(lines) - i):
                    break
                self._append(node, ResolvedNode("resolved", text, None, "", i + 1, ctx.trail, "content"))

    def run_entries(self, parent, names, store_path, ctx, depth, rel, stack) -> None:
        groups = []
        for i, name in enumerate(names):
            if not self._room(parent, depth, len(names) - i):
                break
            res = self._resolve(name, store_path)
            shown = _display(name)
            node = ResolvedNode("resolved", shown, None, "", 0, ctx.trail, _guess_kind(name), "ok")
            if res.state != "ok":
                node.state = res.state
                node.reason = res.reason
                follow: list = []
            else:
                follow = self._open_target(node, res, f"{rel}/{shown}", ctx, depth, ())
            self._append(parent, node)
            groups.append(follow)
        _push(stack, groups)


def _push(stack: list, groups: list) -> None:
    """Push task groups so the first group's first task runs first (LIFO stack)."""
    for tasks in reversed(groups):
        stack.extend(reversed(tasks))


def resolve_tree(lines, *, root_file: str | None = None, limits: Limits | None = None) -> ResolveResult:
    """Resolve the root Write's `lines` (its `outline()`) into a `ResolveResult`.

    `root_file` is the path of the root Write on disk, or None when it has not
    been saved (every well-formed reference is then `unsaved`). The root
    boundary is `real_root(dirname(realpath(root_file)))`. Never raises for
    problems with the referenced files.
    """
    limits = limits or Limits()
    forest = parse_scope_tree(lines)
    if root_file is None:
        root_real = None
        real_file = None
        chain: tuple = ()
        trail = "(unsaved)"
        root = ResolvedNode("root", "(unsaved Write)", None, "", 0, trail, "write", "unsaved",
                            "save the Write to resolve its references")
    else:
        real_file = os.path.realpath(root_file)
        root_real = real_root(os.path.dirname(real_file))
        chain = (chain_key(real_file),)
        trail = os.path.basename(root_file)
        root = ResolvedNode("root", trail, None, "", 0, trail, "write", "ok", "", None, real_file)
    resolver = _Resolver(limits, root_real)
    ctx = _Ctx(root_real, chain, trail)
    stack: list = [("forest", root, forest.roots, ctx, 1, "authored", None)]
    while stack:
        task = stack.pop()
        op = task[0]
        if op == "forest":
            resolver.run_forest(*task[1:], stack)
        elif op == "expand":
            resolver.run_expand(*task[1:], stack)
        else:
            resolver.run_entries(*task[1:], stack)
    resolver.finish()
    return ResolveResult(root, resolver.nodes, resolver.truncated, resolver.warnings, forest.diagnostics, forest.counts)


def iter_nodes(tree):
    """Yield every node of `tree` (a `ResolveResult` or `ResolvedNode`) in pre-order,
    root first, using an explicit stack (safe for very deep trees)."""
    stack = [getattr(tree, "root", tree)]
    while stack:
        node = stack.pop()
        yield node
        stack.extend(reversed(node.children))


_KIND_BADGES = {
    "write": "Write",
    "store": "Store",
    "doc": "Doc (raw text)",
    "prog": "Prog (raw text)",
    "operator": "operator (chain ends)",
    "folder": "[folder]",
}


def badge_text(node: ResolvedNode) -> str:
    """The State-column text for a node: the kind label when ok, else `state: reason`."""
    if node.state == "ok":
        return _KIND_BADGES.get(node.kind, "")
    return f"{node.state}: {node.reason}" if node.reason else node.state


def run_tree(tree) -> list[str]:
    """The Run log: one line per instruction node, depth-first in tree order.

    Each line is `<Keyword> (line N, <trail>): <message>` where the message
    comes from the dud `INSTRUCTIONS` registry ("not implemented yet"), with
    ` [state: reason]` appended when the node is not ok. Executes nothing and
    opens nothing.
    """
    out = []
    for node in iter_nodes(tree):
        if node.keyword is None:
            continue
        instruction = INSTRUCTIONS.get(node.keyword)
        message = instruction(node.value)[1] if instruction else "unknown instruction"
        line = f"{node.keyword} (line {node.line}, {node.trail}): {message}"
        if node.state != "ok":
            line += f" [{node.state}: {node.reason}]" if node.reason else f" [{node.state}]"
        out.append(line)
    return out
