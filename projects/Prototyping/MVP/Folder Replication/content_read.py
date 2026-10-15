"""Per-file text extraction for Folder Replication's content-search half of `sieve()`.

Zero Qt dependency (mirrors `flow.py`'s discipline) - `store_model.py`
imports this module to do the actual file reads behind the content-match
half of its sieve, while staying import-free of PySide6 itself.

Dispatch is purely on the path's own extension (case-insensitive), never on
any other signal (e.g. never on a StoreFile's `.name` heuristics or on
whatever kind of node happens to be calling this).
"""

import sys
from pathlib import Path

# docx_extract.py lives under MVP/File Recompiler/ - reached the same way
# Joint Interactor/operators.py reaches it (sys.path.insert(...) convention), just
# with the root anchored here instead.
_STORE_MVP_ROOT = Path(__file__).resolve().parent
_PROGRAM_MVP_ROOT = _STORE_MVP_ROOT.parent / "File Recompiler"
if str(_PROGRAM_MVP_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROGRAM_MVP_ROOT))

import docx_extract  # noqa: E402 - import must follow the sys.path setup above


def read_content(path: str) -> str:
    """Return the extracted plain-text content of `path`.

    - `.docx` -> File Recompiler's `docx_extract.extract_text()`.
    - `.txt` -> read directly as UTF-8 text.
    - Anything else (unexpected extension) -> returns "" rather than
      raising. The toolbar's file picker (see app_store.py) filters to
      `.docx`/`.txt` only, so this is a defensive fallback, not a primary
      path - a genuinely unreadable/unsupported reference should not be able
      to blow up a sieve() pass over the rest of the tree.
    """
    suffix = Path(path).suffix.lower()
    if suffix == ".docx":
        return docx_extract.extract_text(str(path))
    if suffix == ".txt":
        return Path(path).read_text(encoding="utf-8")
    return ""
