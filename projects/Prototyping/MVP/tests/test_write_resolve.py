"""Tests for write_resolve.py - the iterative, valve-limited reference resolver.

Zero Qt import anywhere in this file. Every file lives under `tmp_path`. The
"never opened" / "no I/O" tests spy on `builtins.open` and the `write_paths`
seams; the recursion tests drop the interpreter's recursion limit to just above
the current stack depth. Junction tests use `write_testkit.make_dir_link` (no
admin rights) and skip cleanly where unsupported. Printed strings go through
`ascii()`.
"""

import builtins
import inspect
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace

_MVP_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_MVP_ROOT / "SAP Modifier"))

import pytest

import write_paths
import write_resolve
import write_testkit
from write_resolve import (
    Limits,
    ResolvedNode,
    ResolveResult,
    badge_text,
    iter_nodes,
    resolve_tree,
    run_tree,
)


@pytest.fixture
def env(tmp_path):
    root = tmp_path / "root"
    fx = write_testkit.tree_fixture(root)
    write_testkit.make_write(root / "main.mwrite", ["Info: main"])  # the root Write exists on disk
    (tmp_path / "outside.txt").write_text("outside", encoding="utf-8")
    return SimpleNamespace(tmp=tmp_path, root=root, fx=fx, main=str(root / "main.mwrite"))


def resolve(env, lines, **kwargs):
    return resolve_tree(lines, root_file=env.main, **kwargs)


def labels(node):
    return [c.label for c in node.children]


def child(node, label):
    for c in node.children:
        if c.label == label:
            return c
    raise AssertionError(f"no child {label!r} in {ascii(labels(node))}")


def max_depth(result):
    deepest = 0
    stack = [(result.root, 0)]
    while stack:
        node, depth = stack.pop()
        deepest = max(deepest, depth)
        stack.extend((c, depth + 1) for c in node.children)
    return deepest


def states(result):
    return [n.state for n in iter_nodes(result)]


def write_chain(directory, count, prefix="w"):
    """`w0 -> w1 -> ... -> w{count-1}`; the last holds a plain text line."""
    for i in range(count):
        target = [f"Info: @{prefix}{i + 1}.mwrite"] if i < count - 1 else ["Info: the end"]
        write_testkit.make_write(directory / f"{prefix}{i}.mwrite", target)


def remove_link_at_teardown(request, link):
    request.addfinalizer(lambda: write_testkit.remove_dir_link(link))


def test_unsaved_root_reports_unsaved_after_the_syntax_check():
    result = resolve_tree(["Phrase: @a.txt", "Info: @C:\\Windows\\win.ini", "Goals: plain", "Limits: @"], root_file=None)
    assert isinstance(result, ResolveResult)
    assert result.root.state == "unsaved" and result.root.trail == "(unsaved)" and result.root.role == "root"
    phrase, info, goals, limits = result.root.children
    assert phrase.state == "unsaved" and phrase.kind == "operator"
    assert info.state == "blocked"  # still blocked, not "unsaved"
    assert goals.state == "ok" and goals.kind == "text"
    assert limits.state == "broken" and "empty" in limits.reason
    assert result.nodes == 4 and not result.truncated
    assert result.counts.clarity == 4


def test_the_chain_ends_at_an_operator(env):
    write_testkit.make_write(env.root / "chain_a.mwrite", ["Info: @chain_b.mwrite"])
    write_testkit.make_write(env.root / "chain_b.mwrite", ["Goals: @a.txt"])
    result = resolve(env, ["Phrase: @chain_a.mwrite"])
    a = child(result.root, "Phrase: @chain_a.mwrite")
    assert (a.kind, a.state, a.role) == ("write", "ok", "authored")
    b = child(a, "Info: @chain_b.mwrite")
    end = child(b, "Goals: @a.txt")
    assert (end.kind, end.state, end.children) == ("operator", "ok", [])
    assert end.trail == "main.mwrite > chain_a.mwrite > chain_b.mwrite"
    assert end.real_path is not None and end.role == "resolved"
    assert badge_text(end) == "operator (chain ends)" and badge_text(a) == "Write"
    assert not result.truncated and result.warnings == []


def test_operator_files_are_never_opened(env, monkeypatch):
    (env.root / "tool.exe").write_bytes(b"MZ\x00\x01 not opened")
    (env.root / "data.bin").write_bytes(b"\x00\xff" * 10)
    write_testkit.make_write(env.root / "holder.mwrite", ["Info: @tool.exe", "Goals: @data.bin", "Limits: @a.txt"])
    opened = []
    real_open = builtins.open

    def spy(file, *args, **kwargs):
        opened.append(os.path.basename(str(file)))
        return real_open(file, *args, **kwargs)

    with monkeypatch.context() as mp:
        mp.setattr(builtins, "open", spy)
        result = resolve(env, ["Phrase: @holder.mwrite", "Meaning: @a.txt", "Respond: @tool.exe"])
    assert "holder.mwrite" in opened  # the spy works and Writes ARE read
    assert not {"tool.exe", "data.bin", "a.txt"} & set(opened), opened
    kinds = [n.kind for n in iter_nodes(result) if n.keyword]
    assert kinds.count("operator") == 5 and all(s == "ok" for s in states(result))


def test_a_nested_write_is_parsed_with_the_same_grammar(env):
    write_testkit.make_write(
        env.root / "nested.mwrite",
        [
            "# a comment",
            "",
            "prose is noise and is not shown",
            "Goals: plain text",
            "Phrase: @sub/b.txt",
            (1, "Info: @a.txt"),
            (2, "Limits: deeper text"),
        ],
    )
    result = resolve(env, ["Meaning: @nested.mwrite"])
    nested = child(result.root, "Meaning: @nested.mwrite")
    assert labels(nested) == ["Goals: plain text", "Phrase: @sub/b.txt"]
    phrase = nested.children[1]
    assert phrase.kind == "operator" and phrase.role == "resolved" and phrase.line == 5
    # authored children under an operator are structural, exactly as at top level
    info = phrase.children[0]
    assert info.state == "structural" and info.role == "authored"
    assert child(info, "Limits: deeper text").state == "structural"
    assert nested.children[0].kind == "text" and nested.children[0].line == 4
    # noise / comments / blanks are not nodes, and the ROOT write's counts are what the result reports
    assert result.counts.clarity == 1 and result.diagnostics == []


def test_store_listing_is_sorted_and_classifies_each_entry(env):
    store = env.root / "shelf.mstore"
    (store / "inner.mstore").mkdir(parents=True)
    (store / "sub").mkdir()
    for name in ("Beta.txt", "alpha.txt", "one.txt", "x.mstore"):
        (store / name).write_text(name, encoding="utf-8")
    (store / "inner.mstore" / "deep.txt").write_text("deep", encoding="utf-8")
    write_testkit.make_write(store / "two.mwrite", ["Info: in two"])
    result = resolve(env, ["Phrase: @shelf.mstore", "Info: @fake.mstore", "Goals: @sub"])
    shelf = child(result.root, "Phrase: @shelf.mstore")
    assert shelf.kind == "store" and shelf.state == "ok"
    assert labels(shelf) == ["alpha.txt", "Beta.txt", "inner.mstore", "one.txt", "sub", "two.mwrite", "x.mstore"]
    kinds = {c.label: c.kind for c in shelf.children}
    assert kinds == {
        "alpha.txt": "operator", "Beta.txt": "operator", "inner.mstore": "store", "one.txt": "operator",
        "sub": "folder", "two.mwrite": "write", "x.mstore": "operator",  # a FILE named like a store is an operator
    }
    assert all(c.role == "resolved" and c.keyword is None for c in shelf.children)
    assert labels(child(shelf, "inner.mstore")) == ["deep.txt"]
    two = child(shelf, "two.mwrite")
    assert labels(two) == ["Info: in two"] and two.children[0].trail == "main.mwrite > shelf.mstore/two.mwrite"
    assert child(shelf, "sub").children == [] and badge_text(child(shelf, "sub")) == "[folder]"
    # a ref straight at a regular file with a store name is an operator; an ordinary dir is a folder leaf
    assert child(result.root, "Info: @fake.mstore").kind == "operator"
    assert child(result.root, "Goals: @sub").kind == "folder"
    # a directory named like a Write is an ordinary folder too
    (env.root / "dir.mwrite").mkdir()
    assert resolve(env, ["Info: @dir.mwrite"]).root.children[0].kind == "folder"


def test_doc_and_prog_files_show_capped_raw_lines_and_are_never_parsed(env):
    (env.root / "raw.mdoc").write_bytes(
        b"Info: @a.txt\n\nlong " + b"x" * 300 + b"\nbell\x07char\r\nlast line\n"
    )
    result = resolve(env, ["Phrase: @raw.mdoc", "Goals: @stub.mprog"])
    doc = child(result.root, "Phrase: @raw.mdoc")
    assert doc.kind == "doc" and doc.state == "ok" and badge_text(doc) == "Doc (raw text)"
    assert [c.kind for c in doc.children] == ["content"] * 5
    assert doc.children[0].label == "Info: @a.txt" and doc.children[0].keyword is None  # inert text, not an instruction
    assert doc.children[0].children == [] and doc.children[0].real_path is None
    assert [c.line for c in doc.children] == [1, 2, 3, 4, 5]
    assert doc.children[1].label == ""
    assert len(doc.children[2].label) == 200
    assert doc.children[3].label == "bellchar"  # the control character is scrubbed
    assert doc.children[4].label == "last line"
    prog = child(result.root, "Goals: @stub.mprog")
    assert prog.kind == "prog" and labels(prog) == ["prog line"]
    # line cap and byte cap mark the doc node truncated with a reason
    (env.root / "many.mdoc").write_text("\n".join(f"line {i}" for i in range(10)), encoding="utf-8")
    capped = resolve(env, ["Info: @many.mdoc"], limits=Limits(max_content_lines=3))
    many = capped.root.children[0]
    assert labels(many) == ["line 0", "line 1", "line 2"] and many.state == "truncated"
    assert "first 3 lines" in many.reason and capped.truncated
    (env.root / "wide.mprog").write_bytes("\u00e9".encode("utf-8") * 100)  # 200 bytes of 2-byte characters
    cut = resolve(env, ["Info: @wide.mprog"], limits=Limits(max_read_bytes=101))  # cuts mid-character
    wide = cut.root.children[0]
    assert wide.state == "truncated" and "MAX_READ_BYTES" in wide.reason
    assert wide.children[0].label == "\u00e9" * 50
    # exactly 3 lines with a trailing newline is not "capped"
    (env.root / "three.mdoc").write_text("a\nb\nc\n", encoding="utf-8")
    exact = resolve(env, ["Info: @three.mdoc"], limits=Limits(max_content_lines=3)).root.children[0]
    assert exact.state == "ok" and labels(exact) == ["a", "b", "c"]
    # unreadable content: one marker child, the doc node itself stays ok
    (env.root / "binary.mdoc").write_bytes(b"text\x00binary")
    (env.root / "bad.mprog").write_bytes(b"\xff\xfe\xfa")
    for name in ("binary.mdoc", "bad.mprog"):
        node = resolve(env, [f"Info: @{name}"]).root.children[0]
        assert node.state == "ok" and len(node.children) == 1, name
        marker = node.children[0]
        assert (marker.label, marker.kind, marker.state) == ("[unreadable]", "marker", "broken")


def test_self_reference_and_a_two_file_cycle(env):
    write_testkit.make_write(env.root / "cycle_a.mwrite", ["Info: @cycle_b.mwrite"])
    write_testkit.make_write(env.root / "cycle_b.mwrite", ["Goals: @cycle_a.mwrite"])
    result = resolve(env, ["Phrase: @main.mwrite", "Meaning: @cycle_a.mwrite"])
    selfref = child(result.root, "Phrase: @main.mwrite")
    assert selfref.state == "cycle" and selfref.kind == "write" and selfref.children == []
    a = child(result.root, "Meaning: @cycle_a.mwrite")
    b = child(a, "Info: @cycle_b.mwrite")
    back = child(b, "Goals: @cycle_a.mwrite")
    assert (a.state, b.state, back.state) == ("ok", "ok", "cycle") and back.children == []
    assert badge_text(back).startswith("cycle:")
    assert not result.truncated  # a cycle is detected, not cut off by a valve


def test_a_diamond_is_not_a_cycle(env):
    write_testkit.make_write(env.root / "left.mwrite", ["Info: @shared.mwrite"])
    write_testkit.make_write(env.root / "right.mwrite", ["Info: @shared.mwrite"])
    write_testkit.make_write(env.root / "shared.mwrite", ["Goals: shared text"])
    result = resolve(env, ["Phrase: @left.mwrite", "Meaning: @right.mwrite", "Limits: @shared.mwrite"])
    assert "cycle" not in states(result) and set(states(result)) == {"ok"}
    for parent in ("Phrase: @left.mwrite", "Meaning: @right.mwrite"):
        shared = child(child(result.root, parent), "Info: @shared.mwrite")
        assert labels(shared) == ["Goals: shared text"]  # expanded on BOTH routes
    assert labels(child(result.root, "Limits: @shared.mwrite")) == ["Goals: shared text"]


def test_a_cycle_through_a_store_and_its_child_is_detected(env):
    store = env.root / "loop.mstore"
    store.mkdir()
    write_testkit.make_write(store / "back.mwrite", ["Info: @../loop.mstore"])
    result = resolve(env, ["Phrase: @loop.mstore"])
    loop = child(result.root, "Phrase: @loop.mstore")
    back = child(loop, "back.mwrite")
    assert loop.state == "ok" and back.state == "ok"
    assert child(back, "Info: @../loop.mstore").state == "cycle"
    assert not result.truncated


def test_a_cycle_through_a_junction_alias_is_caught_by_real_path(env, request):
    alias = env.root / "jn"
    if not write_testkit.make_dir_link(alias, env.root):
        pytest.skip("directory links are not supported here")
    remove_link_at_teardown(request, alias)
    write_testkit.make_write(env.root / "a.mwrite", ["Info: @jn/a.mwrite"])
    result = resolve(env, ["Phrase: @a.mwrite"])
    a = child(result.root, "Phrase: @a.mwrite")
    assert a.state == "ok"
    via_alias = child(a, "Info: @jn/a.mwrite")
    assert via_alias.state == "cycle", ascii((via_alias.state, via_alias.reason))
    assert os.path.normcase(via_alias.real_path) == os.path.normcase(os.path.realpath(env.root / "a.mwrite"))


def test_broken_references_and_invalid_nested_files_do_not_expand(env):
    (env.root / "bad.mwrite").write_text("{ not json", encoding="utf-8")
    (env.root / "newer.mwrite").write_text('{"version": 9, "paragraphs": []}', encoding="utf-8")
    result = resolve(env, ["Phrase: @missing.mwrite", "Info: @bad.mwrite", "Goals: @newer.mwrite", "Limits: @gone/deeper.txt"])
    missing, bad, newer, gone = result.root.children
    assert (missing.state, missing.kind, missing.children) == ("broken", "write", [])
    assert "not found" in missing.reason and badge_text(missing) == "broken: not found"
    assert bad.state == "broken" and bad.reason.startswith("invalid Write:") and bad.children == []
    assert newer.state == "broken" and "version" in newer.reason
    assert gone.state == "broken" and gone.kind == "operator"
    assert not result.truncated


def test_blocked_references_never_touch_the_disk_or_expand(env, monkeypatch):
    hostile = [
        "C:\\Windows\\win.ini", "\\\\evil-host\\share\\x", "//evil-host/share", "../outside.txt", "sub/../../outside.txt",
        "a.txt:ads", "CON", "nul.tar.gz", "/etc/passwd", "a.",
    ]
    lstat_calls = []
    real_lstat = os.lstat
    monkeypatch.setattr(write_paths, "_lstat", lambda p: (lstat_calls.append(str(p)), real_lstat(p))[1])
    result = resolve(env, [f"Phrase: @{h}" for h in hostile])
    assert [n.state for n in result.root.children] == ["blocked"] * len(hostile)
    assert all(n.children == [] and n.real_path is None for n in result.root.children)
    assert lstat_calls == []  # not one filesystem probe for any of them
    assert all("blocked" in badge_text(n) for n in result.root.children)


def test_dotdot_inside_the_root_is_fine_from_a_nested_write_and_escapes_are_blocked(env):
    write_testkit.make_write(env.root / "sub" / "up.mwrite", ["Info: @../a.txt", "Goals: @../../outside.txt", "Limits: @../sub/b.txt"])
    result = resolve(env, ["Phrase: @sub/up.mwrite"])
    up = child(result.root, "Phrase: @sub/up.mwrite")
    ok, escape, sibling = up.children
    assert (ok.state, ok.kind) == ("ok", "operator")
    assert escape.state == "blocked" and "root" in escape.reason
    assert (sibling.state, sibling.kind) == ("ok", "operator")
    assert ok.trail == "main.mwrite > sub/up.mwrite"


def test_max_depth_valve_marks_the_parent_truncated(env):
    write_chain(env.root, 12)
    result = resolve(env, ["Phrase: @w0.mwrite"], limits=Limits(max_depth=5))
    assert result.truncated and max_depth(result) == 5
    cut = [n for n in iter_nodes(result) if n.state == "truncated"]
    assert len(cut) == 1 and "MAX_DEPTH 5" in cut[0].reason and cut[0].children == []
    assert "MAX_DEPTH 5" in badge_text(cut[0])
    # the real constant: a 70-deep chain stops at 64 levels without an error
    write_chain(env.root, 70, prefix="deep")
    long = resolve(env, ["Phrase: @deep0.mwrite"])
    assert long.truncated and max_depth(long) == write_resolve.MAX_DEPTH == 64
    assert any("MAX_DEPTH 64" in n.reason for n in iter_nodes(long) if n.state == "truncated")
    assert long.warnings


def test_max_nodes_valve_counts_dropped_children_and_marks_the_parent(env):
    lines = [f"Goals: text {i}" for i in range(30)]
    result = resolve(env, lines, limits=Limits(max_nodes=10))
    assert result.nodes == 10 and len(result.root.children) == 10 and result.truncated
    assert result.root.state == "truncated" and "MAX_NODES 10; 20 not shown" in result.root.reason
    assert sum(1 for _ in iter_nodes(result)) == 11
    assert result.warnings and "MAX_NODES" in result.warnings[0]
    # content lines of a raw file count against the same budget
    (env.root / "long.mdoc").write_text("\n".join(f"l{i}" for i in range(50)), encoding="utf-8")
    doc = resolve(env, ["Info: @long.mdoc"], limits=Limits(max_nodes=8))
    assert doc.nodes == 8 and doc.root.children[0].state == "truncated"
    assert "MAX_NODES 8; 43 not shown" in doc.root.children[0].reason
    assert resolve(env, lines).nodes == 30  # nothing tripped under the real limits


def test_a_fan_out_bomb_terminates_quickly_and_reads_each_file_once(env, monkeypatch):
    for k in range(12):
        body = [f"Info: @fan_{k + 1}.mwrite"] * 3 if k < 11 else ["Info: leaf"]
        write_testkit.make_write(env.root / f"fan_{k}.mwrite", body)
    reads = []
    real_read = write_resolve.read_capped
    monkeypatch.setattr(write_resolve, "read_capped", lambda p, m: (reads.append(os.path.basename(p)), real_read(p, m))[1])
    started = time.perf_counter()
    result = resolve(env, ["Phrase: @fan_0.mwrite"])
    elapsed = time.perf_counter() - started
    assert elapsed < 1.0, f"took {elapsed:.2f}s"
    assert result.nodes == write_resolve.MAX_NODES == 2000 and result.truncated
    assert any("MAX_NODES" in n.reason for n in iter_nodes(result) if n.state == "truncated")
    assert len(reads) == len(set(reads)) <= 12  # the per-pass cache: each file read once, not once per reference


def test_an_oversize_nested_write_and_the_total_read_budget(env):
    write_testkit.make_write(env.root / "big.mwrite", ["Info: " + "x" * 400])
    result = resolve(env, ["Phrase: @big.mwrite", "Goals: @sub/c.mwrite"], limits=Limits(max_write_bytes=200))
    big = child(result.root, "Phrase: @big.mwrite")
    assert big.state == "truncated" and "MAX_WRITE_BYTES" in big.reason and big.children == []
    assert result.truncated
    small = child(result.root, "Goals: @sub/c.mwrite")  # well under the cap: read normally
    assert small.state == "ok" and labels(small) == ["Info: in c"]
    # the per-pass byte budget: the first read fits, later files (Writes, Docs, Progs) are not read at all
    write_testkit.make_write(env.root / "w1.mwrite", ["Info: one"])
    write_testkit.make_write(env.root / "w2.mwrite", ["Info: two"])
    result = resolve(env, ["Phrase: @w1.mwrite", "Goals: @w2.mwrite", "Limits: @stub.mdoc", "Info: @stub.mprog"],
                     limits=Limits(max_total_read_bytes=1))
    w1, w2, doc, prog = result.root.children
    assert w1.state == "ok" and labels(w1) == ["Info: one"]  # the first read fits (budget is checked before reading)
    for node in (w2, doc, prog):
        assert node.state == "truncated" and "MAX_TOTAL_READ_BYTES" in node.reason and node.children == []
    assert result.truncated and any("MAX_TOTAL_READ_BYTES" in w for w in result.warnings)


def test_the_resolver_survives_a_very_low_recursion_limit(env):
    write_chain(env.root, 40)
    lines = [(i, "Info: @a.txt") for i in range(2000)]
    old_limit = sys.getrecursionlimit()
    sys.setrecursionlimit(len(inspect.stack(0)) + 45)
    try:
        chain = resolve(env, ["Phrase: @w0.mwrite"])
        deep = resolve(env, lines, limits=Limits(max_depth=5000, max_nodes=5000))
        chain_nodes = list(iter_nodes(chain))
        deep_nodes = list(iter_nodes(deep))
        log = run_tree(deep)
    finally:
        sys.setrecursionlimit(old_limit)
    # root + the Phrase line + one line per Write (w0..w39); nothing truncated at 41 levels
    assert chain.nodes == 41 and len(chain_nodes) == 42 and not chain.truncated
    assert max_depth(chain) == 41
    assert max_depth(deep) == 2000 and deep.nodes == 2000 and len(deep_nodes) == 2001 and len(log) == 2000
    assert deep.diagnostics == [] and repr(deep.root.children[0]).startswith("ResolvedNode(")
    assert all(n.state == "structural" for n in deep_nodes[2:])  # under an operator: inert all the way down


def test_authored_children_under_an_operator_or_text_or_noise_are_structural(env):
    lines = [
        "Phrase: @a.txt",
        (1, "Info: @sub/b.txt"),
        (2, "Goals: deeper still"),
        "Limits: some text",
        (1, "Meaning: @a.txt"),
        "just prose",
        (1, "Respond: @a.txt"),
    ]
    result = resolve(env, lines)
    phrase, limits, respond = result.root.children
    assert phrase.kind == "operator" and phrase.state == "ok"
    info = phrase.children[0]
    assert info.state == "structural" and "operator" in info.reason and info.real_path is None
    assert info.kind == "operator"  # a guess from the name; it is never opened or resolved
    assert child(info, "Goals: deeper still").state == "structural"
    assert limits.children[0].state == "structural" and "text instruction" in limits.children[0].reason
    assert respond.state == "structural" and "not an instruction" in respond.reason
    log = run_tree(result)
    assert sum("[structural:" in line for line in log) == 4  # Info, Goals, Meaning, Respond
    # a folder is a leaf too
    folder = resolve(env, ["Phrase: @sub", (1, "Info: @a.txt")])
    assert folder.root.children[0].kind == "folder" and folder.root.children[0].children[0].state == "structural"


def test_authored_children_under_a_broken_blocked_or_unsaved_ref_are_allowed(env):
    lines = [
        "Phrase: @missing.mwrite", (1, "Info: @a.txt"),
        "Goals: @C:\\nope", (1, "Limits: @a.txt"),
    ]
    result = resolve(env, lines)
    broken, blocked = result.root.children
    assert broken.state == "broken" and broken.children[0].state == "ok"
    assert blocked.state == "blocked" and blocked.children[0].state == "ok"
    assert broken.children[0].kind == "operator"
    unsaved = resolve_tree(["Phrase: @a.txt", (1, "Info: @b.txt")], root_file=None)
    assert unsaved.root.children[0].state == "unsaved" and unsaved.root.children[0].children[0].state == "unsaved"
    assert unsaved.root.children[0].children[0].role == "authored"


def test_authored_children_come_before_the_resolved_contents(env):
    result = resolve(env, ["Phrase: @sub/c.mwrite", (1, "Goals: mine"), (1, "Limits: @a.txt")])
    node = result.root.children[0]
    assert labels(node) == ["Goals: mine", "Limits: @a.txt", "Info: in c"]
    assert [c.role for c in node.children] == ["authored", "authored", "resolved"]
    # authored lines belong to the ROOT Write (its trail and folder), resolved ones to the nested Write
    assert [c.trail for c in node.children] == ["main.mwrite", "main.mwrite", "main.mwrite > sub/c.mwrite"]
    assert node.children[1].kind == "operator" and node.children[1].state == "ok"
    # same order for a store: authored first, then the listing
    listing = resolve(env, ["Info: @notes.mstore", (1, "Goals: authored under store")]).root.children[0]
    assert labels(listing)[0] == "Goals: authored under store" and listing.children[1].label == "one.txt"


def test_trail_line_numbers_and_iter_nodes_order(env):
    lines = ["", "# note", "Phrase: @sub/c.mwrite", "", "Info: after", "prose", (1, "Goals: under prose")]
    result = resolve(env, lines)
    root = result.root
    assert root.label == "main.mwrite" and root.trail == "main.mwrite" and root.line == 0
    phrase, after, under_prose = root.children
    assert (phrase.line, after.line, under_prose.line) == (3, 5, 7)  # blanks, comments and noise still count
    assert under_prose.state == "structural" and phrase.trail == "main.mwrite"
    inner = phrase.children[0]
    assert (inner.label, inner.line, inner.trail) == ("Info: in c", 1, "main.mwrite > sub/c.mwrite")
    order = [n.label for n in iter_nodes(result)]
    assert order == ["main.mwrite", "Phrase: @sub/c.mwrite", "Info: in c", "Info: after", "Goals: under prose"]
    assert [n.label for n in iter_nodes(result.root)] == order  # a node works as well as a result
    assert (result.counts.clarity, result.counts.noise, result.counts.comment, result.counts.blank) == (3, 1, 1, 2)


def test_run_tree_logs_one_honest_line_per_instruction_node(env):
    result = resolve(env, ["Phrase: @sub/c.mwrite", "Goals: text", "Info: @missing.txt", "Understanding: @stub.mdoc", "Knowledge: @notes.mstore"])
    log = run_tree(result)
    assert log == [
        "Phrase (line 1, main.mwrite): not implemented yet",
        "Info (line 1, main.mwrite > sub/c.mwrite): not implemented yet",
        "Goals (line 2, main.mwrite): not implemented yet",
        "Info (line 3, main.mwrite): not implemented yet [broken: not found]",
        "Understanding (line 4, main.mwrite): not implemented yet",
        "Knowledge (line 5, main.mwrite): not implemented yet",
        "Info (line 1, main.mwrite > notes.mstore/two.mwrite): not implemented yet",
    ]
    keyworded = [n for n in iter_nodes(result) if n.keyword]
    assert len(log) == len(keyworded) == 7
    # content lines, store entries and markers never appear in the log; nested Writes inside a store do
    assert not any("stub" in line or "one.txt" in line for line in log)
    assert run_tree(result.root) == log  # a node works as well as a result
    assert run_tree(resolve_tree([], root_file=env.main)) == []


def test_badge_text_covers_every_state_and_kind():
    def node(kind="text", state="ok", reason=""):
        return ResolvedNode("authored", "x", None, "", 0, "t", kind, state, reason)

    assert {k: badge_text(node(k)) for k in ("text", "write", "store", "doc", "prog", "operator", "folder", "content", "marker")} == {
        "text": "", "write": "Write", "store": "Store", "doc": "Doc (raw text)", "prog": "Prog (raw text)",
        "operator": "operator (chain ends)", "folder": "[folder]", "content": "", "marker": "",
    }
    for state in ("broken", "blocked", "cycle", "truncated", "unsaved", "structural"):
        assert badge_text(node(state=state)) == state
        assert badge_text(node(state=state, reason="why")) == f"{state}: why"
    # a non-ok state wins over the kind label
    assert badge_text(node("operator", "blocked", "escapes")) == "blocked: escapes"
