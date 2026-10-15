# SAP Modifier (m.1.4.0) - Script Processor with Live Special Instructions

A PySide6 prototype, additive alongside `../File Recompiler/`, `../Joint Interactor/` and
`../Folder Replication/`: the fourth container/type, a **text/script processor** - "a doc
with operators on it, just a text-processor instead of a canvas". It proves four
things end to end:

1. Rich script editing with seven tools: **Bold, Line, Dash, Italics, Spread,
   Embed, Indent**.
2. Eight instructions carried as typed text - Phrase, Limits, Meaning, Goals,
   Respond, Understanding, Info, Knowledge (the octagon read clockwise from
   North). They are duds, but honest ones: Run logs "not implemented yet".
3. **Special instructions** (Write / Store / Doc / Program files) nested inside a
   Write as text, scoped by indentation, with the chain ending at an
   **operator** (any other file: a leaf that cannot hold anything).
4. Live, relative-path references that resolve recursively and safely.

Nothing in this app executes a file or touches the network: no `eval`/`exec`,
no subprocess, no `startfile`, no sockets. `tests/test_write_static.py` enforces
that by scanning the source (AST), not by trusting this sentence.

## Running it

From the shared root `.venv` (`Prototyping/.venv`):

```
Prototyping/.venv/Scripts/python.exe "Prototyping/MVP/SAP Modifier/app_write.py"
```

Then use the toolbar "Open" on `Prototyping/MVP/sample/write_demo/main.mwrite`.
The demo folder is generated - see "The demo fixtures" below. Opening `main.mwrite`
shows one line per instruction type and, in the tree pane, a chain that ends at an
operator, a cycle, broken and blocked references, a Store, a Doc, a Prog, a
diamond and a fan-out that trips the `MAX_NODES` valve on purpose.

## Module layout

Everything lives in `Prototyping/MVP/SAP Modifier/`. Every basename is globally unique
(all MVP folders share one flat `sys.path` inside one pytest process), hence the
`write_` prefix. Dependency direction: Qt-free model -> widgets -> thin
`app_write.py` (the Folder Replication layering).

| File | Responsibility | Qt |
|---|---|---|
| `write_paths.py` | The security core. `SI_EXTENSIONS` (the ONLY place the four special-instruction extensions are spelled), the pure-string reference syntax check, the folder-boundary policy, the stepwise safe resolver `resolve_ref`, `relative_ref_for`. | free |
| `write_model.py` | Plain dataclasses (`Run`, `EmbedRun`, `Paragraph`, `WriteDocument`), tag algebra (Italics cycle, toggle rule, spread/indent clamps), the text sanitiser, `EmbedRegistry`, `outline()`, plain-text export. | free |
| `write_format.py` | The `.mwrite` JSON: strict loader/validator, PNG header check, atomic saver. The **only** module that writes files (plus the build-time sample generator). | free |
| `write_grammar.py` | The eight keywords, the dud instruction registry, `classify_line`, `count_lines`, the iterative `parse_scope_tree`, completion helpers. | free |
| `write_resolve.py` | The iterative, valve-limited resolver, `ResolvedNode`, `iter_nodes`, `run_tree`, `badge_text`, `Limits`. | free |
| `write_docmodel.py` | Property ids, tags <-> `QTextCharFormat`, `document_to_model` / `model_to_document`. | Qt |
| `write_overlay.py` | Run-segment geometry and painter for what Qt cannot do natively (Dash diagonal, left lean), as free functions shared with Embed's snapshot. | Qt |
| `write_embed.py` | Snapshot render, PNG encode/decode with caps, embed / un-embed. | Qt |
| `write_highlight.py` | `WriteHighlighter`: instruction colours and live reference-state colours (foreground and underline only). | Qt |
| `write_editor.py` | `WriteEditor(QTextEdit)`: tools, paste guard, completer, keys, `paintEvent`. Written in a later step - see the plan; verified in the review pass. | Qt |
| `write_tree.py` | `TreePane(QTreeWidget)`: the read-only resolved-tree pane. Imports only `write_resolve` (types) and Qt. | Qt |
| `app_write.py` | `MainWindow` (title "Go - SAP Modifier: Script Processor"; the editor beside a tree pane over a read-only Run log; toolbar; a status widget with Clarity / Noise / Comments and Modified / Saved), `main()`, `SAMPLE_DIR = _WRITE_MVP_ROOT.parent / "sample"`. Written in a later step - see the plan; verified in the review pass. | Qt |
| `../sample/generate_write_samples.py`, `../sample/write_demo/` | The idempotent fixture generator (`build_samples(out_dir)`) and its committed output. | free |
| `../tests/write_testkit.py`, `../tests/test_write_*.py` | Test helpers (incl. the Qt lifetime helpers) and the test files. | mixed |

Dependencies: `write_paths`, `write_model`, `write_grammar` import only the stdlib;
`write_format` -> `write_model`; `write_resolve` -> `write_paths`, `write_grammar`,
`write_format`, `write_model`; `write_docmodel` -> `write_model`, `write_format`;
`write_overlay` -> `write_docmodel`; `write_embed` -> `write_model`,
`write_format`, `write_docmodel`, `write_overlay`; `write_highlight` ->
`write_grammar`, `write_paths`; `write_tree` -> `write_resolve`; the editor and the
app sit on top of all of them.

## Interaction model

The editor and window behaviour below is the plan (the SAP Modifier plan,
`mvp-chat-4-inherited-origami.md`, in the user's `.claude/plans/`); items marked
"(plan)" live in `write_editor.py` / `app_write.py` and are verified in the review
pass. Everything else is described from the modules on disk (their code for the
Qt-free modules and `write_tree.py`, their docstrings for the other Qt modules).

### Lines, instructions and scope

- **Line classes** (`write_grammar.classify_line`, decided from the text only; the
  indent is an int on the paragraph, never whitespace): blank -> ignored; `# ...`
  -> a **comment** (must start in column 0; dimmed, ignored, counted); `Keyword:
  value` with one of the eight keywords (ASCII case-insensitive, the colon
  **immediately** after the keyword, keyword in column 0) -> an **instruction**
  ("clarity"); anything else non-blank -> **noise**. A value that starts with `@`
  is a **reference** (the rest, trimmed, is the relative path; empty -> "empty
  reference"), otherwise a plain-text value. The status bar shows Clarity / Noise /
  Comments (the record's parser accept/reject boundary) (plan).
- **Scope tree**: one node per instruction line. The parent is the nearest
  preceding instruction with a strictly smaller indent (a jump such as 0 -> 3 is
  legal, with a diagnostic). Blank and comment lines are transparent. Authored
  children may sit only under a special-instruction **reference**; under an
  operator, a text value or a noise line they are a **structural error**, visible
  in the tree and never resolved.
- **Resolution** (`write_resolve`): every reference goes through `resolve_ref`.
  Exactly one state per node - `ok | broken | blocked | cycle | truncated |
  unsaved | structural` - and one kind - `text | write | store | doc | prog |
  operator | folder | content | marker`. A **Write** is loaded and its own
  instruction lines expanded with the same grammar. A **Store** (a directory)
  expands to its listing (SI-extension entries recurse, ordinary files are
  operator leaves, ordinary sub-folders are opaque `[folder]` leaves). A **Doc** or
  **Prog** is shown as raw UTF-8 lines (read-only, capped, never parsed) or
  `[unreadable]`. Any other existing file is an **operator**: never opened, the
  chain ends. Nothing is copied: it is all re-read on every pass ("live").
- **An SI node's children** are the lines **authored** under it (indentation)
  followed by the target's own contents **resolved** live; the tree pane marks both
  (below).
- **Run** (F5, plan) walks the resolved tree depth-first and logs `"<Keyword> (line
  N, <chain of Writes>): not implemented yet"` per instruction node, with ` [state:
  reason]` appended when the node is not ok (`write_resolve.run_tree`, verified).
  It executes nothing and opens nothing.
- **The tree pane** (`write_tree.TreePane`, verified): read-only; columns
  "Instruction / file" and "State"; the State text is `write_resolve.badge_text`
  (the kind label - `Write`, `Store`, `Doc (raw text)`, `Prog (raw text)`,
  `[folder]`, `operator (chain ends)` - when ok, else `state: reason`), coloured per
  state (ok teal, broken red, blocked orange, unsaved grey-blue - the editor's own
  reference colours - plus cycle purple, truncated mustard, structural brown; an ok
  operator or folder is muted grey so a healthy chain end never looks like an error).
  A problem row also tints its label.
- **Authored vs resolved** are marked in two ways: each row stores its role
  (`root` / `authored` / `resolved`), and below the root Write's own top-level lines
  the label carries an ASCII marker - `+ ` for a line authored under a reference,
  `~ ` (italic, blue-grey) for content resolved from the reference's target.
- **Expansion policy**: a result of 300 nodes or fewer is expanded completely;
  a bigger one is expanded through depth 2 only (the root Write is depth 0), so a
  fan-out result opens as a readable outline. The rebuild is one explicit-stack loop
  with updates disabled, replaces the previous contents entirely, and keeps the
  scroll position.

### The seven tools and the editor (plan)

- **Bold, Line (strike-through), Dash, Italics, Spread / Narrow, Embed / Un-embed,
  Indent / Outdent** on a toolbar, Ctrl+B, Ctrl+I, Tab / Shift+Tab. Bold/Line/Dash
  over a selection apply to all if any character lacks the tag and remove only if
  all have it; an empty selection toggles the typing format. Every tool is exactly
  **one undo step**.
- **Italics** cycles upright -> lean right -> lean left -> upright. Right lean is
  the font's native italic; left lean is custom-painted (`write_overlay`).
- **Spread** widens letter-spacing of the selection (absolute px, additive, clamped
  to -3.0 .. 32.0); Narrow is the same control in the other direction.
- **Embed** replaces a selection (or the current paragraph) with an inline PNG
  snapshot of it; the **original text is kept underneath** and Un-embed restores
  it (per `write_embed`'s design: the picture is display-only, the tree, counts and
  Run derive from the source text).
- **Indent** sets the paragraph's scope depth (an int, `MAX_INDENT` 32); Enter
  inherits it. **Outdent** (Shift+Tab / Shift-click Indent) is an addition of mine.
- **Keyword autocomplete**: typing at the start of a line offers the eight
  keywords; accepting one inserts `Keyword: ` as one undo step.
- **Insert reference...** (file picker) and **Insert Store reference...** (D10)
  refuse when the Write is unsaved or the target is outside the boundary.
- **Refresh** is debounced 250 ms after real edits and immediate on Save, window
  activation and the Refresh button. Closing with unsaved changes prompts
  Save / Discard / Cancel. Open reads and validates first; on a refused file the
  path, editor and file are untouched.

### Decisions D1-D10 (defaults I chose; each is one constant or one method to veto)

The plan numbers them D1-D10 but lists no D3.

- **D1 - nested Write size.** A nested `.mwrite` may be up to 8 MiB
  (`MAX_WRITE_BYTES`); the 1 MiB cap (`MAX_READ_BYTES`) applies only to raw
  `.mdoc` / `.mprog` reads. A single 1 MiB cap would make any Write containing an
  embed un-nestable.
- **D2 - a text-valued instruction cannot nest.** `Goals: finish the draft` cannot
  carry indented children (a strict reading of "scope begins at an SI");
  `write_grammar.TEXT_INSTRUCTIONS_CAN_NEST = False`, flagged `under_text` and
  shown as `structural`.
- **D4 - a multi-paragraph selection cannot be Embedded.** `EmbedRun.source` is a
  single-paragraph run list; a multi-paragraph range is refused with a status
  message (`write_embed`; only the "select a whole line" gesture, a selection that
  ends exactly at the start of the next paragraph, is trimmed to its own
  paragraph). Alternative: a schema change.
- **D5 - Ctrl+I is right lean only.** The toolbar Italics button runs the full
  upright -> right -> left cycle (plan). `write_model.next_italic` is the cycle.
- **D6 - the Run log's "path" is the chain of containing Writes**
  (`main.mwrite > sub/a.mwrite`; a file inside a Store extends it with
  `store.mstore/file`) - the `trail` on every resolved node.
- **D7 - Narrow may go below zero.** Letter-spacing runs down to -3 px
  (`SPREAD_MIN`) so Narrow is never a dead button.
- **D8 - embed snapshots render at DPR 1** in the run's own formatting (no live
  syntax colours), so the file is deterministic and resolution-independent; the
  price is a blurry picture on a HiDPI screen.
- **D9 - the clipboard is plain text only.** Copy, cut, paste and drop lose their
  formatting, and an embed copies as its source text (plan); the stored PNG is
  display-only. `write_model.sanitize_text` removes control characters and U+FFFC
  and, for paste, keeps newlines so a pasted block still splits into paragraphs;
  the paste guard, `loadResource` refusing anything but `mem://` and the "no
  `setHtml` / `insertHtml` anywhere" rule are the editor's part (plan).
- **D10 - "Insert Store reference..." toolbar action.** A file dialog cannot pick
  a directory, so a Store gets its own directory-picker action (plan).

### Small documented choices

- **Keyword and colon.** The keyword must be followed **immediately** by `:`
  (`Phrase : x` is noise), and leading whitespace is not skipped (`  Goals: x` is
  noise - scope is what the Indent tool sets).
- **No escape for a leading `@`.** A text value that starts with `@` is a
  reference; there is no way to write a literal one.
- **A Store must be a directory.** A regular file named `x.mstore` is an operator;
  a directory named `x.mwrite` is an ordinary folder.
- **Store listing** is sorted case-insensitively (`casefold`, then name). Ordinary
  sub-folders are opaque `[folder]` leaves: they are not expanded.
- **Unresolved references.** The kind of a `broken`/`blocked`/`unsaved` reference
  is a best guess from the name (`.mwrite` -> write, else operator), and authored
  children under such a node are allowed (its kind is unknown), unlike under a
  real operator where they are `structural`.
- **No rebasing of references on Save As.** References stay as typed (forward
  slashes) and are re-evaluated against the new folder; Save As says so when some
  would go non-ok (plan).
- **`unsaved` state.** A well-formed reference in an unsaved Write is `unsaved`,
  but only AFTER the syntax check, so a hostile reference is still `blocked`.
- **Atomic save, and a refused file is never overwritten.** `save_write` writes a
  temp file in the target's folder, fsyncs, then `os.replace`s; before touching
  the disk it re-parses its own bytes and refuses to save a file the loader would
  reject (e.g. more than `MAX_EMBEDS` embeds).
- **Diagnostics in the tree.** Grammar diagnostics of the root Write (an indent
  jump, an indented first line) are shown as a tooltip on the line they are
  attached to, not as extra rows.
- **Labels are scrubbed for display.** Control, format (bidi override, zero-width)
  and surrogate characters in a label or reason show as `?`, and text is cut at 300
  characters; the tooltip carries the rest.
- **The demo opens small; the fan-out has its own file.** `main.mwrite` resolves in well under
  the valve. The 3^k fan-out lives in `fanout_demo.mwrite`: open it to watch the `MAX_NODES`
  valve trip and mark one node `truncated` on purpose.

## Path policy and safety valves

**The folder is the boundary.** A reference must be a relative path that resolves
inside the folder of the root Write (the one open in the window). Nested Writes
resolve their own references relative to their own folder, and `..` from a nested
Write is legal as long as the result stays inside that root.

Blocked - and blocked means **no filesystem access at all** (`check_ref_syntax` is
a pure string function that runs first; a test monkeypatches the `lstat` /
`readlink` / `realpath` seams to raise and proves it):

- absolute paths (`/x`, `\x`), drive-letter paths (`C:\x`, `C:x`), UNC and device
  paths (`\\host\share\x`, `\\?\...`, `\\.\pipe`) - merely `stat`-ing a UNC path
  makes Windows try to authenticate to the host over SMB;
- alternate data streams and every `:` and any of `<>"|?*`;
- reserved device names on the part before the first dot (`CON`, `PRN`, `AUX`,
  `NUL`, `COM0-9`, `LPT0-9`, incl. superscript digits, so `CON.txt` and
  `nul.tar.gz` too), and a trailing dot or space in a name;
- control (Cc), format (Cf - zero-width, bidi overrides) and surrogate characters;
- a reference longer than `MAX_REF_LEN` 1024, more than `MAX_REF_PARTS` 64
  components, or a component longer than `MAX_COMPONENT_LEN` 255;
- any `..` that climbs out of the root (a lexical pre-check, then re-checked while
  walking).

An empty reference is `broken` ("empty reference"), not blocked.

`os.path.isabs` is never trusted (it is False for `\\host\share` and `/foo`, which
both escape) and nothing untrusted is ever `join`-ed before the syntax check. The
walk is "openat"-style: one component at a time, `lstat` each, and a symlink or an
NTFS junction is never followed by the OS - its target is read, vetted (UNC or
device -> blocked without contacting it; outside the root -> blocked) and spliced
back into the pending components, at most `MAX_LINK_HOPS` 16 times. Containment is
decided on strings (normcase + normpath + `commonpath`), so the sibling-prefix trap
(`root2` vs `root`) and cross-drive paths are classified without any I/O.

**Cycle detection** is by real path along the current ancestor chain only, so a
junction that aliases a Write is caught, while a **diamond** (one file reached by
two routes) is legal and expands both times.

**Safety valves** (`write_resolve.Limits`; a tripped valve marks the PARENT node
`truncated` with the reason and never raises):

| Constant | Value | Meaning |
|---|---|---|
| `MAX_DEPTH` | 64 | levels of nesting |
| `MAX_NODES` | 2,000 | nodes below the root |
| `MAX_READ_BYTES` | 1 MiB | one raw Doc/Prog read |
| `MAX_WRITE_BYTES` | 8 MiB | one nested Write (D1) |
| `MAX_TOTAL_READ_BYTES` | 32 MiB | everything read in one pass |
| `MAX_LISTING_SCAN` | 5,000 | directory entries scanned per Store |
| `MAX_CONTENT_LINES` / `MAX_CONTENT_LINE_CHARS` | 200 / 200 | raw lines shown per Doc/Prog, and their width |

A per-pass cache (each file read and parsed once, each listing scanned once, each
`(reference, folder)` resolved once) is what lets a fan-out bomb finish quickly;
it lives for one `resolve_tree` call, so the next pass is live. The resolver is
one explicit-stack loop, so a 2000-deep chain cannot exhaust Python's stack.

**Format caps** (`write_model` / `write_format`): `MAX_FILE_BYTES` 8 MiB;
`MAX_PARAGRAPHS` 20,000; `MAX_RUNS_PER_PARAGRAPH` 2,000; `MAX_TOTAL_RUNS` 200,000;
`MAX_RUN_TEXT` 100,000; `MAX_INDENT` 32; spread -3.0 .. 32.0; embeds:
`MAX_EMBEDS` 200, `MAX_EMBED_PNG_BYTES` 2 MiB, `MAX_TOTAL_EMBED_BYTES` 16 MiB,
`MAX_IMAGE_DIM` 4096, `MAX_IMAGE_PIXELS` 4,000,000 - a PNG's size is read from its
IHDR bytes and refused **before** any image library sees it, and
`QImageReader.setAllocationLimit` (process-global) is never called. Snapshot
limits (`write_embed`): `SNAPSHOT_MAX_W` 2000, `SNAPSHOT_MAX_H` 1000,
`SNAPSHOT_MAX_PIXELS` 1,500,000, `SNAPSHOT_MAX_CHARS` 50,000. Loading is strict:
strict UTF-8, no `NaN`/`Infinity`, no duplicate keys, genuine ints for `version`
and `indent`, a known tag with a bad value is an error (unknown tags are ignored),
control characters are scrubbed with a warning. Other constants: highlighter cache
`MAX_CACHE` 4096; tree pane `EXPAND_ALL_MAX_NODES` 300, `PARTIAL_EXPAND_DEPTH` 2,
`MAX_TEXT_CHARS` 300; the editor's paste cap and the 250 ms refresh debounce are in
the plan.

Known residuals, accepted: a TOCTOU window between the check and the later read; a
root folder that itself lives on a network share; hard links (indistinguishable
from the file they alias); the resolver runs on the UI thread.

## The demo fixtures

`sample/write_demo/` is generated by `sample/generate_write_samples.py`
(`build_samples(out_dir)`, idempotent, deterministic: no timestamps, sorted order,
LF line endings, the canonical JSON `write_format` produces) and committed.
Regenerate with:

```
Prototyping/.venv/Scripts/python.exe "Prototyping/MVP/sample/generate_write_samples.py"
```

| Fixture | What it shows |
|---|---|
| `main.mwrite` | one line per instruction type in octagon order (Phrase ... Knowledge) referencing everything below, plus an indented authored line under the Store reference, a text-valued instruction, a `#` comment and a noise line (Clarity 10 / Noise 1 / Comments 1). Bold, italic (right and left), Line, Dash and Spread are used on some runs. |
| `chain_a` -> `chain_b` -> `chain_c` (`.mwrite`) -> `ends_here.txt` | a chain that ends at an operator |
| `cycle_a` <-> `cycle_b` | a reference cycle, flagged once |
| `broken_ref.mwrite` | a missing file, a missing folder, an empty reference |
| `blocked_ref.mwrite` | `@../outside.txt`, `@C:\Windows\win.ini`, `@\\host\share\x`, an alternate data stream and a reserved name - INERT TEXT in a JSON file; the generator never creates a file, folder or reserved device name for them |
| `notes.mstore/` | a DIRECTORY: `one.txt`, `two.mwrite`, `sub/deep.txt` (git cannot commit an empty folder) |
| `stub.mdoc`, `stub.mprog` | plain UTF-8 text, shown raw and never parsed (the `Prog` holds an inert `Info: @...` line) |
| `fan_0` .. `fan_11` (`.mwrite`) | each references the next three times, so the tree grows as 3^k and trips `MAX_NODES` |
| `diamond_top` -> `diamond_left`, `diamond_right` -> `diamond_leaf` | **an addition to the plan's fixture list**, because its Verification section lists a diamond scenario: one shared leaf reached twice is NOT a cycle. `main.mwrite` references it as an authored line under the Store reference |
| `.gitattributes` | **an addition**: `* -text`. This machine's Git has `core.autocrlf=true`, which would rewrite every fixture to CRLF on checkout and break the byte-for-byte test on a fresh clone |

The generator never deletes anything, so files from an older generator version
stay in a reused folder until removed by hand.

## Reused from other MVPs

Patterns only - **Write imports nothing from a sibling MVP** (enforced by
`test_write_static.py`).

- **Folder Replication**: the Qt-free-model -> widgets -> thin-app layering, this README's
  template, the "documented judgment call" convention, and `SAMPLE_DIR = ...parent
  / "sample"` in the entry point.
- **File Recompiler** `instructions.py`: the dud instruction registry
  (`INSTRUCTIONS = {name: fn}` returning `(None, "not implemented yet")`); the
  message is bare because the Run log supplies the `<Keyword> (line N, path):`
  prefix.
- **Joint Interactor**: keeping the model Qt-free (`flow.py`), the 250 ms debounce value
  (`live_sync.py`, `DEBOUNCE_MS`; a local constant in the Write app - plan), and the
  monkeypatch-a-Qt-dialog-through-a-module-attribute test pattern
  (`tests/test_operators.py`).

## Qt lifetime rules

Every MVP's tests run in ONE process against ONE session `QApplication`, so widgets
an earlier test file leaked are still alive, with their `deleteLater()` events still
queued, while a Write test runs. The rules the SAP Modifier follows:

- In production widgets connect **bound methods** of QObject subclasses
  (`button.clicked.connect(self._on_clicked)`), **not** lambdas that close over
  `self`: a bound-method connection does not keep the wrapper alive, so no
  reference cycle through C++ forms. Parent child widgets to their owner.
- Every Write test that builds widgets or spins the event loop requests the
  `write_qt` fixture (`pytestmark = pytest.mark.usefixtures("write_qt")`), and every
  top-level widget it creates goes through `track_qt(widget)`, which the fixture
  deletes deterministically at teardown. The fixture's setup runs
  `settle_leftover_qt()` first.
- **No module-level Qt objects** (widgets, documents, highlighters, painters,
  pixmaps, timers, fonts): they outlive every test and are built before the
  `QApplication` exists. Plain value types (`QColor`, `QRectF`, `QPointF`) are fine;
  never a `QFont`.
- Keep a `QPainter` short (`painter.end()` in a `finally`); never touch a document,
  layout, block or fragment wrapper after the widget that owns it is gone; a
  `QSyntaxHighlighter` dies with its document.
- **Never use `QTextDocument.clone()` or `shiboken6.delete` on documents.**
  `clone()` returns a wrapper Python does not own, registered against the original
  document; deleting it by hand leaves a stale wrapper that crashes the process
  (access violation) when the editor's own document is destroyed later. The embed
  snapshot uses a plain `QTextDocument()` filled from a `QTextDocumentFragment`.
  (`write_testkit.dispose_qt` uses `shiboken6.delete` on top-level widgets only.)

## Upstream fix in Folder Replication (`rows.py`)

Folder Replication's `rows.py` closes over `self` in lambdas
(`clicked.connect(lambda ...: self...)`), which builds a reference cycle through
C++ that `gc` cannot free. When `RowStack` rebuilds its rows with `deleteLater()`
and the Qt event loop later spins in the same process - any `QTest.qWait`,
`processEvents` or `sendPostedEvents(None, DeferredDelete)` runs EVERY queued delete
in the process - the old row's buttons die, their closures drop the last reference
to the owner's wrapper, and PySide deletes the owner from inside the destructor of
its own child: **an access violation (exit 139)** in the Write tests after
`test_rows.py`. It was bisected to `test_rows.py::test_set_root_resets_open_path_by_default`
followed by any event spin (no Write code needed). The original tests pass only by
file order (`test_live_sync.py`, the one original file that spins the loop, sorts
before `test_rows.py`); run `test_rows.py` first and it crashed on its own, on unmodified code.

**Fixed at the source.** `RowStack._build_entry` now connects callbacks that close
over a `weakref.ref` to the stack instead of over `self` (23 lines added, 2 removed
in `Folder Replication/rows.py`, behaviour identical while the stack is alive). Proof: the
two ORIGINAL files that crashed, `test_rows.py` then `test_live_sync.py`, now give
20 passed, exit 0, with no Write code involved, and the full suite passes with
`test_rows.py` first, with the Write files first, and in normal order.

The Write tests keep their own workaround as defence in depth:
`write_testkit.settle_leftover_qt()` pins a reference to every top-level widget while
it flushes the queued deletes (`tests/test_write_lifetime.py` reproduces the hazard
and guards the helper).

## Known limits

- **Building a 2,500-entry Store takes about 3.7 s on the UI thread.** Every entry
  goes through `resolve_ref` (an `lstat` walk and a `realpath`), which is a safety
  choice - a listed name gets the same vetting as a typed one - so it is noted, not
  silently "fixed" (the listing is capped at `MAX_LISTING_SCAN` 5,000).
- **Overlay limits** (`write_overlay`): left-to-right text only; a straight-underline
  redraw (the highlighter's wavy or dotted underline is not reproduced inside a
  left-lean or Dash run); no per-glyph tilt inside ligatures (a piece is drawn as one
  string); cover rectangles are advance boxes rounded outward, so ink that overhangs
  can survive as a hairline; the caret does not blink inside a tilted run (plan).
- **A text value starting with `@` has no escape.**
- **No rebasing of references on Save As** (they are re-evaluated instead).
- **Embed snapshots are blurry on HiDPI** (D8).
- **Tree pane**: rebuilding a 2,000-node result takes roughly 0.2 s on the UI thread
  (measured on this machine, on top of the resolve itself). Real trees are at most
  about `MAX_DEPTH` 64 levels deep, but Qt's own destruction of an artificial
  2,000-deep chain (the stress test) takes about 2 s inside `clear()`.
- **Embed spoofing**: the stored PNG can show text different from its `source`;
  today everything reads `source`, so this is display-only - consider re-rendering on
  load once instructions execute.

## Out of scope

- **Executing anything.** The eight instructions are duds; Run is an honest log.
  Nothing opens an operator; `.mdoc` / `.mprog` are shown, never parsed or run.
- **Rich clipboard** - plain text only (D9).
- **Multi-paragraph embeds** (D4), embed nesting, right-to-left text in the overlay.
- **Rebasing references on Save As**, following references outside the root
  folder, network paths.
- **QtWebEngine** and any browser surface.
- **Refactoring other MVPs**: the one exception is the small `rows.py` fix above.

## Tests

Live in the shared `Prototyping/MVP/tests/` directory. Every file anchors itself
(`_MVP_ROOT = Path(__file__).resolve().parent.parent`, then `sys.path.insert(0,
_MVP_ROOT / "SAP Modifier")`), the Qt-free modules are tested with zero PySide6 import,
Qt tests use the shared `qapp` fixture from `conftest.py` **plus** `write_qt`, nothing
writes into `sample/` (temp files go under `tmp_path`), `QTest.qWait` is used
instead of `time.sleep`, dialogs are stubbed by module-attribute monkeypatch, and
printed strings go through `ascii()` (a Windows cp1252 console cannot print U+FFFC).

| File | Purpose |
|---|---|
| `write_testkit.py` | helpers, no tests: `make_write`, `fake_png_header`, `make_dir_link` / `remove_dir_link` (an NTFS junction needs no admin rights), `tree_fixture`, and the Qt lifetime helpers (`write_qt`, `track_qt`, `dispose_tracked`, `settle_leftover_qt`) |
| `test_write_static.py` | AST scan, nothing imported or run: no process / dynamic-code / network APIs, Qt-free modules import no PySide, only `write_format.py` (and the generator) writes files, SI extension literals only in `write_paths.py`, basenames globally unique and every source ASCII |
| `test_write_paths.py` | syntax rules, boundary containment, the stepwise resolver, blocked = no I/O (monkeypatched seams), junctions, UNC link targets, link loops |
| `test_write_model.py` | tag algebra, sanitiser, run coalescing, outline and plain export, `EmbedRegistry` |
| `test_write_format.py` | round trip, atomic save, every rejection, a rejected file's bytes unchanged |
| `test_write_grammar.py` | line classes, the scope-tree edge table, 2000-deep input without recursion, completion helpers |
| `test_write_resolve.py` | states and kinds, cycle vs diamond, every valve, the fan-out bomb, a low recursion limit, `run_tree` |
| `test_write_docmodel.py` | document <-> model round trip, highlighter formats never leak into tags, merged image fragments |
| `test_write_overlay.py` | the never-shown-editor crash canary, visible-only scan, Dash and left-lean pixels (ratio and band-centroid assertions) |
| `test_write_highlight.py` | keyword / value / reference-state colours, foreground and underline only, the `rehighlight()` no-`contentsChange` canary |
| `test_write_editor.py` | configuration, hostile-HTML paste, the tools, one undo step per tool, indent rules, completer (in the editor step) |
| `test_write_embed.py` | embed / un-embed, refusals, snapshot pixels and caps, corrupt PNG degrades to source text |
| `test_write_tree.py` | `TreePane`: badges and colours for every state and kind, read-only rows, a 2000-deep chain built without recursion, the expansion policy, replacing the previous contents, scroll position |
| `test_write_app.py` | the window: Open / Save / Save As, dirty prompts, debounced refresh, insert-reference guards, Run log (in the app step) |
| `test_write_samples.py` | the generator reproduces the committed fixtures byte for byte (and is idempotent); chain / cycle / broken / blocked / diamond states, Store / Doc / Prog kinds, and the fan-out trips `MAX_NODES` - all resolved against a `tmp_path` COPY of the committed tree |
| `test_write_lifetime.py` | regression tests for the Qt lifetime helpers (the order-dependent crash above) |

Run one file, all the Write tests, or the whole suite:

```
Prototyping/.venv/Scripts/python.exe -m pytest Prototyping/MVP/tests/test_write_tree.py -q
Prototyping/.venv/Scripts/python.exe -m pytest Prototyping/MVP/tests/test_write_*.py -q
Prototyping/.venv/Scripts/python.exe -m pytest Prototyping/MVP/tests -q
```

(In PowerShell a `*` glob is not expanded for you; list the files or use a POSIX
shell.) Add `-p no:cacheprovider` where a clean tree matters. Pixel tests assert
ratios and band centroids inside one image, never absolute positions, and skip when
a platform draws no ink: **the offscreen platform used in the tests has no fonts**
(Qt prints "Cannot find font directory"), so every glyph, spaces included, is a
solid-looking box. If a fixture is changed on purpose, regenerate the folder (above)
and re-run `test_write_samples.py`.
