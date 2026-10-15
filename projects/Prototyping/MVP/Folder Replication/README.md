# Folder Replication - Folder/File Store with Live Keyword Sieve

A PySide6 prototype, additive alongside `../File Recompiler/` and `../Joint Interactor/`:
an in-memory folder/file store (`.docx`/`.txt` file references, arbitrarily
nested folders) browsed via an "open-upward" row-stack widget, plus a live
keyword search bar that renders a pruned, green-highlighted copy of the tree
above the real one as you type. This is the first MVP prototype to
intentionally build folder/filetype mechanics - every earlier MVP app
explicitly disclaims that as out of scope; here it's the whole point.

## Running it

From the shared root `.venv` (`Prototyping/.venv`):

```
Prototyping/.venv/Scripts/python.exe "Prototyping/MVP/Folder Replication/app_store.py"
```

## Module layout

| File | Responsibility |
|---|---|
| `store_model.py` | Zero-Qt pure data model: `StoreFolder`/`StoreFile`, `add_folder`/`add_file`/`delete_item`, `sieve()`. |
| `content_read.py` | Per-file text extraction for content search (`.docx` via File Recompiler's `docx_extract`, `.txt` via plain read). |
| `rows.py` | `RowStack` - the open-upward row-stack `QWidget`, one instance per pane. |
| `app_store.py` | Entry point: `QMainWindow`, keyword bar, top/bottom panes, toolbar, status bar. |

## Interaction model

- **Open-upward row stack** (`rows.py`): the root folder's children render
  in a row at the BOTTOM of the pane. Clicking a folder opens a new row
  directly ABOVE the row it was clicked in, showing that folder's children.
  Clicking the same folder again collapses that row (and everything stacked
  above it). Clicking a *different* folder on the same row replaces what was
  open there, which naturally collapses anything that had been stacked
  above the old choice. Clicking a file does nothing (files have no
  children to open). Only one open folder's contents per row, by
  construction.
- **Top pane** (search results): appears only once the keyword bar is
  non-empty. Live search - fires on every keystroke, including a single
  letter. Shows a pruned COPY of the tree (via `store_model.sieve()`):
  only folders that lead to at least one match survive, plus the matching
  files themselves, with every folder rendered in green. A match is a
  case-insensitive substring match against a file's own name or its
  extracted content. This pane resets its own open/drilled-down state on
  every keystroke, since the pruned tree's shape can change completely from
  one keystroke to the next.
- **Bottom pane** (the real tree): always present, never filtered. This IS
  the "noise" view - there is no separate noise-only pane, per spec; the
  full unfiltered tree already serves that purpose.
- **Current-directory rule** (documented judgment call, not spec-mandated):
  "current directory" - i.e. where "New Folder" and "Add File(s)..." insert
  new items - is tracked *only* from the bottom pane's currently-open
  folder (or the real root, if nothing is open there). The top/search
  pane's open-state is ignored entirely for this purpose, since it's a
  transient browsing surface, not a navigation surface. See the comment
  next to `MainWindow._current_directory` in `app_store.py`.
- **Toolbar**: "New Folder" (name prompt via `QInputDialog`) and
  "Add File(s)..." (multi-select file picker filtered to `.docx`/`.txt`,
  defaulting to `Prototyping/MVP/sample/`) - both target the current
  directory as defined above, then rebuild the bottom pane (always) and the
  top pane too if a keyword is currently active, so a newly added item is
  immediately searchable rather than waiting for the next keystroke.
- **Right-click a row**: "Delete" - removes that folder/file from the
  store. Offered only in the bottom (real, full-tree) pane; see
  `rows.RowStack`'s `enable_delete` docstring for why it's intentionally
  not offered in the top/search pane (deleting a row there would only
  remove it from that pane's transient pruned copy, not from the real
  store, and it would silently reappear on the next keystroke's re-search).
  Deleting a folder that is itself part of the bottom pane's currently-open
  chain collapses that chain back to the deleted folder's own row, so the
  next rebuild never tries to render a deleted folder's children.

## Reused from other MVPs

- `docx_extract.extract_text()` from `../File Recompiler/`, imported via the
  same `sys.path.insert(...)` convention `Joint Interactor/operators.py` established
  (see `content_read.py`).
- General conventions borrowed from `Joint Interactor/`: keeping `store_model.py`
  Qt-free (mirrors `flow.py`), the `QMainWindow` + toolbar + status bar
  shape of `app_canvas.py`, and the `QFileDialog.getOpenFileNames` (plural)
  convention from `File Recompiler/app_multi.py` for "Add File(s)...".

## Out of scope

- **Rename** - not implemented anywhere in this prototype.
- **Move-between-folders** - not implemented anywhere in this prototype.
- **Persistence / save-load** - the entire store is session-only, in
  memory; closing the app discards it.
- **A separate "noise-only" pane** - the bottom pane's full, unfiltered
  view already IS the noise view; there is no third pane.

## Tests

Live in the shared `Prototyping/MVP/tests/` directory (not a separate
per-prototype folder): `test_store_model.py` (zero Qt) and `test_rows.py`
(uses the shared `qapp` fixture from `conftest.py`).
