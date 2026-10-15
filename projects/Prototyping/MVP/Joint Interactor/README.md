# Joint Interactor - Free-Placement Operator Canvas

A PySide6 prototype, additive alongside `../File Recompiler/`: a blank,
free-placement `QGraphicsScene`/`QGraphicsView` canvas where `.docx` and
`.txt` operator nodes can be placed anywhere and wired together freely
(real chaining/fan-out/fan-in, not capped to a single fixed pipeline like
File Recompiler). Each wire is independently configured across five toggleable
signal-flow options - **On**, **Off**, **Uni**, **Bi**, **One-shot** -
rather than one fixed mode, and more than one can be set at once.

## Running it

From the shared root `.venv` (`Prototyping/.venv`):

```
Prototyping/.venv/Scripts/python.exe "Prototyping/MVP/Joint Interactor/app_canvas.py"
```

## Module layout

| File | Responsibility |
|---|---|
| `app_canvas.py` | Entry point: `QMainWindow` + canvas, toolbar ("Add Docx Operator", "Add Txt Operator", checkable "Draw Wire" mode), status bar. |
| `operators.py` | `OperatorNode` base (`QGraphicsObject`) + `DocxOperatorNode`/`TxtOperatorNode`. |
| `wires.py` | `Wire` (`QGraphicsPathItem`), owns a `WireState`-driven `LiveSync`, right-click toggle menu, shape-driven rendering. |
| `flow.py` | Pure precedence/resolution logic (`WireState`, `resolve()`, `execute()`) - zero Qt dependency, fully unit-testable. |
| `export_paths.py` | Default export-path derivation (symmetric, same-type), format-aware write dispatch, and the non-destructive same-path safety guard. |
| `docx_write.py` | `write_docx(text, path)` - the reverse of File Recompiler's `docx_extract.extract_text()`. |
| `live_sync.py` | `LiveSync` - one wire's `QFileSystemWatcher` + debounce `QTimer`. |
| `Predictions on Options.md` | The toggle-combinatorics analysis: how 32 raw boolean combinations collapse to 5 distinct wire "shapes". |

## Interaction model

- **Add Docx/Txt Operator**: file picker, defaults to `Prototyping/MVP/sample/`.
- **Draw Wire mode** (checkable toolbar toggle): click a source node, then
  click a target node - no drag-handles.
- **Right-click a node**: "Run" (designate as signal source, fire every
  attached wire) / "Export" (re-save to the existing export path, no dialog)
  / "Export As..." (override this node's export path via a native Save-As
  dialog; rejects and re-prompts if the chosen path collides with any
  operator's original source, and immediately writes the current content to
  the newly chosen path) / "Display Contents" (toggles - label flips to
  "Hide Contents" while open - an embedded, live-editable panel showing this
  node's `content`; edits there mark the node dirty and are protected from
  being overwritten by an unrelated reload/incoming wire until explicitly
  discarded) / "Reload from Source" (force-reloads `content` from disk,
  discarding any pending edit) / "Delete".
- **Right-click a wire**: checkable menu toggling each of the 5 independent
  options (On/Off/Uni/Bi/One-shot).

## Non-destructive writes

Every node has an `export_path`, defaulted at construction to be same-type
as its own source (an `.export` infix, e.g. `sample_input.docx` ->
`sample_input.export.docx`) - symmetric across node kinds, depending only on
the source path's own extension, never on node kind. This default is freely
overridable per-node via "Export As...". Write format dispatch is decided
purely by the export path's own extension (never node kind) - see
`export_paths.py`. The `assert_not_original()` guard still only protects
against colliding with any operator's real original source; it does not
also guard against two different nodes' export targets colliding with each
other, an accepted simplification for this MVP. This holds even under
Bi/On (live, bidirectional) wires: no original source is ever overwritten
in place, including anything under `Prototyping/MVP/sample/`.

## Reused from File Recompiler

Only `docx_extract.extract_text()` is imported from `../File Recompiler/`
(via the same `sys.path.insert(...)` convention already established in
`Prototyping/MVP/tests/test_docx_extract.py`). Nothing under
`File Recompiler/` is modified by this build.

## Tests

Live in the shared `Prototyping/MVP/tests/` directory (not a separate
per-prototype folder): `test_flow.py`, `test_export_paths.py`,
`test_docx_write.py`, `test_operators.py`, `test_wires.py`,
`test_live_sync.py`, plus a new `conftest.py` providing the headless
`qapp` fixture.
