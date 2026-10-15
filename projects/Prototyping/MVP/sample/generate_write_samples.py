"""Generate the committed SAP Modifier demo fixtures in `sample/write_demo/`.

Run it directly (`Prototyping/.venv/Scripts/python.exe
"Prototyping/MVP/sample/generate_write_samples.py"`) to (re)build the folder next
to this file, or call `build_samples(out_dir)` from a test to build the same tree
somewhere else (`tests/test_write_samples.py` builds into `tmp_path` and compares
it byte for byte with the committed copy - nothing in a test ever writes into
`sample/`). It is a build-time tool, so `test_write_static.py` lets it write files
(the one exception to "only `write_format.py` writes"); `.mwrite` files still go
through `write_format.save_write` so the fixtures are exactly what the app itself
would save.

The demo is one root Write, `main.mwrite`, that points at everything else:

- `chain_a -> chain_b -> chain_c` (`.mwrite`) `-> ends_here.txt`: a chain that ends
  at an operator (any other file: a leaf that is never opened);
- `cycle_a <-> cycle_b`: a reference cycle (flagged, never followed twice);
- `broken_ref`: references that do not resolve (missing file, missing folder, empty);
- `blocked_ref`: references the path policy refuses - `@../outside.txt`,
  `@C:\\Windows\\win.ini`, `@\\\\host\\share\\x`, an alternate data stream and a
  reserved device name. They are INERT TEXT inside a JSON file: this generator
  never creates a file, folder or name for them (and never a reserved device name
  such as nul or con), so resolving them touches nothing;
- `notes.mstore/` (a DIRECTORY: `one.txt`, `two.mwrite`, `sub/deep.txt` - the
  files are what makes it committable, git cannot hold an empty folder);
- `stub.mdoc` and `stub.mprog`: plain UTF-8 text, shown raw and never parsed;
- `fan_0 .. fan_11` (`.mwrite`): every file references the next one three times,
  so the resolved tree grows as 3^k and trips the `MAX_NODES` valve;
- ADDITION beyond the plan's fixture list, because its Verification section
  lists a diamond scenario: `diamond_top -> diamond_left, diamond_right ->
  diamond_leaf`. The shared leaf is reached twice by two routes; that is legal
  and must NOT be reported as a cycle. `main.mwrite` references it as an AUTHORED
  line indented under the Store reference, which also shows authored children
  sitting before the target's resolved contents;
- ADDITION: `.gitattributes` (`* -text`). This machine's Git has
  `core.autocrlf=true`, which would rewrite every fixture to CRLF on checkout and
  break the byte-for-byte test on a fresh clone; `-text` pins the bytes.

`main.mwrite` has one line per instruction type in octagon order (Phrase, Limits,
Meaning, Goals, Respond, Understanding, Info, Knowledge), plus a text-valued
instruction, a `#` comment and a noise line. Bold, italic (right and left), Line,
Dash and Spread are used on some runs so the demo shows the editor's tools.
Documented choice: the fan-out is NOT in `main.mwrite` (its Knowledge line is plain
text pointing at it), so the demo opens with a small tree. The 3^k fan-out lives in
`fanout_demo.mwrite`, whose one Knowledge line references `fan_0`: open that file to
watch the `MAX_NODES` valve trip and mark one node `truncated` on purpose.

Determinism: no timestamps, no randomness, sorted write order, LF line endings,
the canonical JSON `write_format.encode_write` produces. Running it twice yields
identical bytes. It never deletes anything: files from an older generator version
stay in a reused folder until removed by hand.

Explicitly out of scope: reading or validating the fixtures (the tests do that),
embeds (no PNG fixture), and anything outside `out_dir`.
"""

import sys
from pathlib import Path

_MVP_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_MVP_ROOT / "SAP Modifier"))

from write_format import save_write
from write_model import Paragraph, Run, WriteDocument
from write_paths import ext_for

MWRITE = ext_for("write")
MSTORE = ext_for("store")
MDOC = ext_for("doc")
MPROG = ext_for("prog")

DEMO_DIR = Path(__file__).resolve().parent / "write_demo"
FAN_LENGTH = 12  # fan_0 .. fan_11; each file (but the last) references the next one 3 times

_BOLD = {"bold": True}


def _line(keyword: str, value: str, indent: int = 0, key_tags=None, value_tags=None) -> Paragraph:
    """`Keyword: value` as two runs (keyword + colon, then the value), so tags can differ."""
    return Paragraph(indent, [Run(f"{keyword}:", dict(key_tags or {})), Run(f" {value}", dict(value_tags or {}))])


def _plain(text: str, indent: int = 0, tags=None) -> Paragraph:
    return Paragraph(indent, [Run(text, dict(tags or {}))])


def _write(*paragraphs: Paragraph) -> WriteDocument:
    return WriteDocument(list(paragraphs))


def _main() -> WriteDocument:
    return _write(
        _plain("# SAP Modifier demo: press Refresh, then F5 (Run), and read the tree on the right.", tags={"italic": "right"}),
        _line("Phrase", f"@chain_a{MWRITE}", key_tags=_BOLD),
        _line("Limits", f"@cycle_a{MWRITE}", key_tags={"bold": True, "italic": "right"}),
        _line("Meaning", f"@broken_ref{MWRITE}", key_tags=_BOLD),
        _line("Goals", f"@blocked_ref{MWRITE}", key_tags=_BOLD),
        _line("Respond", f"@notes{MSTORE}", key_tags=_BOLD),
        _line("Info", f"@diamond_top{MWRITE}", indent=1, key_tags={"italic": "left"}),
        _line("Understanding", f"@stub{MDOC}", key_tags={"bold": True, "spread": 3.0}),
        _line("Info", f"@stub{MPROG}", key_tags=_BOLD),
        _line("Knowledge", f"the MAX_NODES valve demo is its own file: open fanout_demo{MWRITE}", key_tags=_BOLD),
        _line("Goals", "finish the draft", value_tags={"dash": True}),
        _plain("this line is noise: prose is not an instruction", tags={"line": True}),
    )


def _fixtures() -> dict:
    """Relative path -> `WriteDocument` (saved through `write_format`) or text (`str`, UTF-8, LF)."""
    files: dict = {}
    files["main" + MWRITE] = _main()

    files["chain_a" + MWRITE] = _write(_plain("# chain_a -> chain_b -> chain_c -> ends_here.txt"), _line("Meaning", f"@chain_b{MWRITE}"))
    files["chain_b" + MWRITE] = _write(_line("Goals", f"@chain_c{MWRITE}"))
    files["chain_c" + MWRITE] = _write(_line("Respond", "@ends_here.txt"))
    files["ends_here.txt"] = "An operator: a Write can point at this file, but nothing ever opens it.\nThe chain ends here.\n"

    files["cycle_a" + MWRITE] = _write(_line("Understanding", f"@cycle_b{MWRITE}"))
    files["cycle_b" + MWRITE] = _write(_line("Info", f"@cycle_a{MWRITE}"))

    files["broken_ref" + MWRITE] = _write(
        _line("Info", f"@missing_target{MWRITE}"),
        _line("Knowledge", "@no_such_folder/nothing.txt"),
        _line("Phrase", "@"),
    )

    files["blocked_ref" + MWRITE] = _write(
        _plain("# Inert text: none of these paths is ever touched."),
        _line("Info", "@../outside.txt"),
        _line("Knowledge", "@C:\\Windows\\win.ini"),
        _line("Phrase", "@\\\\host\\share\\x"),
        _line("Limits", "@notes.txt:hidden"),
        _line("Meaning", "@CON.txt"),
    )

    files["notes" + MSTORE + "/one.txt"] = "The first note in the Store.\n"
    files["notes" + MSTORE + "/two" + MWRITE] = _write(_line("Info", "a Write that lives inside a Store"))
    files["notes" + MSTORE + "/sub/deep.txt"] = "Ordinary sub-folders of a Store are opaque: this file is never listed.\n"

    files["stub" + MDOC] = f"A {MDOC} file is a recognised special instruction.\nIt is shown as raw text and never parsed.\n"
    files["stub" + MPROG] = (
        f"A {MPROG} file is a recognised special instruction.\n"
        "It is shown as raw text and never run.\n"
        "Info: @not_a_reference.txt   <- inert: nothing inside a Prog is parsed\n"
    )

    for k in range(FAN_LENGTH - 1):
        nxt = f"@fan_{k + 1}{MWRITE}"
        files[f"fan_{k}{MWRITE}"] = _write(*(_line(name, nxt) for name in ("Phrase", "Meaning", "Info")))
    files[f"fan_{FAN_LENGTH - 1}{MWRITE}"] = _write(_line("Info", "the bottom of the fan-out (the MAX_NODES valve trips long before this)"))

    files["fanout_demo" + MWRITE] = _write(
        _plain("# Opens a 3^k fan-out on purpose: the resolver stops at MAX_NODES (2000) and marks the node truncated."),
        _line("Knowledge", f"@fan_0{MWRITE}", key_tags=_BOLD),
    )

    files["diamond_top" + MWRITE] = _write(_line("Phrase", f"@diamond_left{MWRITE}"), _line("Limits", f"@diamond_right{MWRITE}"))
    files["diamond_left" + MWRITE] = _write(_line("Meaning", f"@diamond_leaf{MWRITE}"))
    files["diamond_right" + MWRITE] = _write(_line("Goals", f"@diamond_leaf{MWRITE}"))
    files["diamond_leaf" + MWRITE] = _write(_line("Info", "the shared leaf: reached twice, never a cycle"))

    files[".gitattributes"] = "* -text\n"
    return files


def build_samples(out_dir) -> list[str]:
    """Write every fixture under `out_dir` and return the sorted relative paths (POSIX slashes).

    Creates `out_dir` (and sub-folders) as needed, overwrites files it owns with
    identical bytes on every run and touches nothing else. Raises whatever the
    filesystem raises (no partial-failure handling: it is a build tool).
    """
    root = Path(out_dir)
    root.mkdir(parents=True, exist_ok=True)
    fixtures = _fixtures()
    written = []
    for rel in sorted(fixtures):
        target = root.joinpath(*rel.split("/"))
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = fixtures[rel]
        if isinstance(payload, WriteDocument):
            save_write(target, payload)
        else:
            target.write_bytes(payload.encode("utf-8"))
        written.append(rel)
    return written


if __name__ == "__main__":
    names = build_samples(DEMO_DIR)
    print(f"Wrote {len(names)} fixture files to {ascii(str(DEMO_DIR))}")
