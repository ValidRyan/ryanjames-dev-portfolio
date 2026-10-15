"""Tests for the SAP Modifier demo fixtures (`sample/write_demo/`) and their generator.

Zero Qt. The committed folder is only ever READ: the generator is run into
`tmp_path`, and every resolver scenario runs against a `tmp_path` COPY of the
committed tree (with a real `outside.txt` placed above it as a control, so the
`../outside.txt` reference is proven blocked rather than merely missing). The
first test snapshots the committed tree's bytes AND modification times before and
after everything else it does, which is how "nothing in a test writes into
`sample/`" is checked rather than assumed. Printed strings go through `ascii()`.
"""

import builtins
import shutil
import sys
import time
from pathlib import Path

_MVP_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_MVP_ROOT / "SAP Modifier"))
sys.path.insert(0, str(_MVP_ROOT / "sample"))

import pytest

import generate_write_samples
import write_paths
from write_format import encode_write, read_write_file
from write_grammar import INSTRUCTION_NAMES, LineCounts, classify_line
from write_resolve import MAX_NODES, Limits, badge_text, iter_nodes, resolve_tree, run_tree

FIXTURES = _MVP_ROOT / "sample" / "write_demo"
MW = write_paths.ext_for("write")
MSTORE = write_paths.ext_for("store")
MDOC = write_paths.ext_for("doc")
MPROG = write_paths.ext_for("prog")

EXPECTED_FILES = sorted(
    [
        ".gitattributes",
        "ends_here.txt",
        f"main{MW}",
        f"fanout_demo{MW}",
        f"chain_a{MW}",
        f"chain_b{MW}",
        f"chain_c{MW}",
        f"cycle_a{MW}",
        f"cycle_b{MW}",
        f"broken_ref{MW}",
        f"blocked_ref{MW}",
        f"stub{MDOC}",
        f"stub{MPROG}",
        f"notes{MSTORE}/one.txt",
        f"notes{MSTORE}/two{MW}",
        f"notes{MSTORE}/sub/deep.txt",
        f"diamond_top{MW}",
        f"diamond_left{MW}",
        f"diamond_right{MW}",
        f"diamond_leaf{MW}",
    ]
    + [f"fan_{k}{MW}" for k in range(12)]
)


def tree_of(root: Path) -> tuple:
    """`(files, dirs)`: relative POSIX path -> bytes, and the set of relative directories (dot files included)."""
    files: dict = {}
    dirs: set = set()
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root).as_posix()
        if path.is_dir():
            dirs.add(rel)
        else:
            files[rel] = path.read_bytes()
    return files, dirs


def stamps_of(root: Path) -> dict:
    return {p.relative_to(root).as_posix(): p.stat().st_mtime_ns for p in sorted(root.rglob("*"))}


@pytest.fixture
def demo(tmp_path):
    """A `tmp_path` copy of the committed fixtures (the committed tree is never resolved in place)."""
    root = tmp_path / "demo"
    shutil.copytree(FIXTURES, root)
    (tmp_path / "outside.txt").write_text("a real file above the demo folder", encoding="utf-8")
    return root


def resolve(root: Path, name: str = f"main{MW}", **kwargs):
    lines = read_write_file(root / name).document.outline()
    return resolve_tree(lines, root_file=str(root / name), **kwargs)


def find(node, label_prefix):
    for candidate in iter_nodes(node):
        if candidate.label.startswith(label_prefix):
            return candidate
    raise AssertionError(f"no node starting {label_prefix!r}")


def test_generator_reproduces_the_committed_fixtures_byte_for_byte(tmp_path):
    committed_files, committed_dirs = tree_of(FIXTURES)
    committed_stamps = stamps_of(FIXTURES)

    written = generate_write_samples.build_samples(tmp_path / "built")
    files, dirs = tree_of(tmp_path / "built")
    assert files == committed_files and dirs == committed_dirs, "regenerate: run sample/generate_write_samples.py"
    assert written == sorted(files) == EXPECTED_FILES
    assert dirs == {f"notes{MSTORE}", f"notes{MSTORE}/sub"}, "no empty folders (git cannot commit them)"

    # idempotent: a second run over the same folder changes nothing (a fresh-folder run is what
    # was just compared with the committed tree, so determinism across runs is covered too)
    assert generate_write_samples.build_samples(tmp_path / "built") == written
    assert tree_of(tmp_path / "built") == (files, dirs)
    assert not [rel for rel in files if rel.endswith(".tmp") or ".write-" in rel], "no temp file left behind"

    # the fixtures are what the app itself would save, in LF, with legal names
    for rel, data in files.items():
        assert b"\r" not in data, f"{rel} must be LF only"
        assert write_paths.check_ref_syntax(rel).ok, f"{ascii(rel)} is not a legal relative name"
        if rel.endswith(MW):
            loaded = read_write_file(tmp_path / "built" / rel)
            assert loaded.warnings == [], rel
            assert encode_write(loaded.document) == data, f"{rel} is not canonical write_format output"
    assert files[".gitattributes"] == b"* -text\n"

    # nothing above touched the committed tree (bytes and modification times)
    assert tree_of(FIXTURES) == (committed_files, committed_dirs)
    assert stamps_of(FIXTURES) == committed_stamps


def test_chain_cycle_broken_blocked_and_diamond_states(demo, tmp_path, monkeypatch):
    # chain: write -> write -> operator, and the operator (ends_here.txt) is never opened
    real_open = builtins.open
    opened = []

    def spy_open(file, *args, **kwargs):
        opened.append(str(file))
        return real_open(file, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(builtins, "open", spy_open)
        chain = resolve(demo, f"chain_a{MW}")
    to_b = chain.root.children[0]
    to_c = to_b.children[0]
    end = to_c.children[0]
    assert [n.kind for n in (to_b, to_c, end)] == ["write", "write", "operator"]
    assert (end.label, end.state, end.children) == ("Respond: @ends_here.txt", "ok", [])
    assert badge_text(end) == "operator (chain ends)"
    assert chain.nodes == 3 and not chain.truncated
    assert any(p.endswith(f"chain_c{MW}") for p in opened) and not any("ends_here" in p for p in opened)

    # cycle: cycle_a -> cycle_b -> cycle_a is flagged once, at the point it would close
    cycle = resolve(demo, f"cycle_a{MW}")
    assert [n.state for n in iter_nodes(cycle)].count("cycle") == 1 and cycle.nodes == 2
    closing = find(cycle.root, "Info: @cycle_a")
    assert closing.state == "cycle" and closing.children == [] and "cycle" in closing.reason

    # broken: three unresolved references, none of them a crash or a silent plain-text fallback
    broken = resolve(demo, f"broken_ref{MW}")
    assert [(n.state, n.reason) for n in broken.root.children] == [
        ("broken", "not found"),
        ("broken", "not found"),
        ("broken", "empty reference"),
    ]

    # blocked: five refused syntaxes; a spy proves not one of them reaches lstat/readlink
    def refuse(path):
        raise AssertionError(f"a blocked reference touched the filesystem: {ascii(path)}")

    with monkeypatch.context() as patch:
        patch.setattr(write_paths, "_lstat", refuse)
        patch.setattr(write_paths, "_readlink", refuse)
        blocked = resolve(demo, f"blocked_ref{MW}")
    assert len(blocked.root.children) == 5
    assert all(n.state == "blocked" and n.real_path is None and n.reason for n in blocked.root.children)
    reasons = [n.reason for n in blocked.root.children]
    assert "escapes" in reasons[0] and "drive" in reasons[1] and "unc" in reasons[2]
    assert "illegal" in reasons[3] and "reserved" in reasons[4]
    assert (tmp_path / "outside.txt").exists()  # the control: the real file above the folder stays blocked
    assert not (demo / "outside.txt").exists() and not list(demo.glob("nul*")) and not list(demo.glob("con*"))

    # diamond: one shared leaf reached by two routes is legal - NOT a cycle
    diamond = resolve(demo, f"diamond_top{MW}")
    leaves = [n for n in iter_nodes(diamond) if n.label.startswith("Info: the shared leaf")]
    assert len(leaves) == 2 and leaves[0] is not leaves[1]
    assert not any(n.state == "cycle" for n in iter_nodes(diamond)) and not diamond.truncated
    assert diamond.nodes == 6  # left, leaf, leaf text, right, leaf, leaf text

    # ...and the same states, all at once, when the demo's own main.mwrite is opened
    full = resolve(demo)
    tally = {}
    for n in iter_nodes(full):
        tally[n.state] = tally.get(n.state, 0) + 1
    assert (tally["cycle"], tally["blocked"], tally["broken"]) == (1, 5, 3)
    assert "structural" not in tally and "unsaved" not in tally
    assert find(full.root, "Phrase: @chain_a").children[0].children[0].children[0].kind == "operator"
    assert not any(n.state == "cycle" for n in iter_nodes(find(full.root, "Info: @diamond_top")))


def test_store_doc_and_prog_kinds(demo):
    result = resolve(demo)
    store = find(result.root, "Respond: @notes")
    assert (store.kind, store.state, badge_text(store)) == ("store", "ok", "Store")
    # the authored line comes first, then the listing (case-insensitive order); sub-folders are opaque
    assert [(c.role, c.label) for c in store.children] == [
        ("authored", f"Info: @diamond_top{MW}"),
        ("resolved", "one.txt"),
        ("resolved", "sub"),
        ("resolved", f"two{MW}"),
    ]
    one, sub, two = store.children[1:]
    assert (one.kind, sub.kind, two.kind) == ("operator", "folder", "write")
    assert one.children == [] and sub.children == [] and badge_text(sub) == "[folder]"
    assert [c.label for c in two.children] == ["Info: a Write that lives inside a Store"]
    assert not any("deep.txt" in n.label for n in iter_nodes(store))

    doc = find(result.root, "Understanding: @stub")
    assert (doc.kind, doc.state, badge_text(doc)) == ("doc", "ok", "Doc (raw text)")
    doc_lines = (demo / f"stub{MDOC}").read_text(encoding="utf-8").splitlines()
    assert [c.label for c in doc.children] == doc_lines
    assert all(c.kind == "content" and c.keyword is None and c.children == [] for c in doc.children)

    prog = find(result.root, "Info: @stub")
    assert (prog.kind, prog.state, badge_text(prog)) == ("prog", "ok", "Prog (raw text)")
    prog_lines = (demo / f"stub{MPROG}").read_text(encoding="utf-8").splitlines()
    assert [c.label for c in prog.children] == prog_lines
    inert = prog.children[-1]
    assert inert.label.startswith("Info: @not_a_reference.txt")
    assert (inert.keyword, inert.ref, inert.state, inert.kind, inert.children) == (None, None, "ok", "content", [])
    assert not any("not_a_reference" in line for line in run_tree(result)), "raw text is never parsed, so never Run"


def test_fan_out_trips_max_nodes_and_the_root_counts_are_as_designed(demo):
    started = time.perf_counter()
    result = resolve(demo, f"fanout_demo{MW}")
    elapsed = time.perf_counter() - started
    assert elapsed < 2.0, f"the fan-out bomb must terminate quickly, took {elapsed:.2f}s"
    assert result.truncated and result.nodes == MAX_NODES == 2000
    assert any("MAX_NODES" in w for w in result.warnings)

    # the valve trips only inside the fan (the last instruction); everything before it resolves completely
    fan = find(result.root, "Knowledge: @fan_0")
    inside_fan = {id(n) for n in iter_nodes(fan)}
    tripped = [n for n in iter_nodes(result) if n.state == "truncated"]
    assert tripped and all(id(n) in inside_fan for n in tripped)
    assert all(n.reason.startswith("MAX_NODES 2000; ") and n.reason.endswith("not shown") for n in tripped)

    # the same fan alone under a small budget trips at exactly that budget (3^k growth, depth is not the limit)
    alone = resolve(demo, f"fan_0{MW}", limits=Limits(max_nodes=100))
    assert alone.truncated and alone.nodes == 100
    assert not any(n.state == "truncated" and "MAX_DEPTH" in n.reason for n in iter_nodes(alone))

    # each fan file references the next one exactly three times; the last one references nothing
    for k in range(12):
        outline = read_write_file(demo / f"fan_{k}{MW}").document.outline()
        refs = [classify_line(text).ref_text for _indent, text in outline if classify_line(text).is_ref]
        assert refs == ([f"fan_{k + 1}{MW}"] * 3 if k < 11 else []), k

    # main.mwrite itself opens SMALL now: nothing truncated, well under the valve
    result = resolve(demo)
    assert not result.truncated and result.nodes < 200

    # root counts and layout of main: 8 octagon lines in order + an indented authored line + a text
    # instruction (10 clarity), one comment, one noise line
    assert result.counts == LineCounts(clarity=10, noise=1, comment=1, blank=0) and result.diagnostics == []
    keywords = [c.keyword for c in result.root.children]
    assert keywords[:8] == list(INSTRUCTION_NAMES) and keywords[8:] == ["Goals"]
    assert find(result.root, "Goals: finish the draft").kind == "text"
    document = read_write_file(demo / f"main{MW}").document
    used = {(key, value) for p in document.paragraphs for run in p.runs for key, value in run.tags.items()}
    assert {("bold", True), ("italic", "right"), ("italic", "left"), ("dash", True), ("line", True), ("spread", 3.0)} <= used
    assert [p.indent for p in document.paragraphs].count(1) == 1
