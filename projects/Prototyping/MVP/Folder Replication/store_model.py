"""Pure data model for the Folder Replication folder/file tree.

Zero Qt dependency, by design - mirrors `flow.py`'s discipline: plain
dataclasses and functions, fully unit-testable with no QApplication needed
(`test_store_model.py` never imports PySide6). `rows.py` (Qt) owns the
widget/rendering layer and calls into this module, the same relationship
`wires.py` has to `flow.py`.

This module MAY import `content_read.py` (also Qt-free - just file I/O) for
the content-search half of `sieve()`'s match test. That keeps `store_model`
itself pure/testable while still doing real file reads, since
`content_read.py` has no Qt in it either.

Files are references only: a `StoreFile.path` points at a real on-disk file
that is NEVER moved, copied, or written to by anything in this module - same
non-destructive ethos as `Joint Interactor/export_paths.py`, even though there is no
export/write concept here at all.

Explicitly out of scope: rename, move-between-folders. Do not add those
functions here at all.
"""

from dataclasses import dataclass, field
from pathlib import Path

import content_read


@dataclass
class StoreFile:
    """A reference to a real on-disk file - never moved/copied/written to.

    `name` is derived from the real path (`Path(path).name`) at creation
    time via `add_file()`; it is not re-derived later, so it stays stable
    even if the underlying file were somehow renamed on disk out from under
    the store (an edge case this prototype does not otherwise guard against).
    """

    name: str
    path: str


@dataclass
class StoreFolder:
    """A folder node. `children` is a plain list of StoreFolder/StoreFile
    instances in arrival order (plain list append order) - never reordered
    or sorted anywhere in this module."""

    name: str
    children: list = field(default_factory=list)


def new_root() -> StoreFolder:
    """Construct a fresh, empty root folder (the implicit unnamed root,
    `name=""`). The store owns exactly one of these; folders nest under it
    arbitrarily via `add_folder()`."""
    return StoreFolder(name="")


def add_folder(parent: StoreFolder, name: str) -> StoreFolder:
    """Create a new child `StoreFolder` named `name`, append it to
    `parent.children`, and return it. Pass the real root as `parent` when
    there is no "current directory" open."""
    folder = StoreFolder(name=name)
    parent.children.append(folder)
    return folder


def add_file(parent: StoreFolder, path: str) -> StoreFile:
    """Create a new child `StoreFile` referencing the real on-disk `path`,
    append it to `parent.children`, and return it. `name` is derived from
    `path` via `Path(path).name`. The file itself is never copied/moved -
    only referenced."""
    store_file = StoreFile(name=Path(path).name, path=str(path))
    parent.children.append(store_file)
    return store_file


def delete_item(parent: StoreFolder, item) -> None:
    """Remove `item` from `parent.children`.

    No rename, no move - not in scope, do not add those here.

    Documented choice: this is a no-op (does not raise) if `item` is not
    actually present in `parent.children`, so callers (e.g. `rows.py`'s
    right-click Delete handler) can call this defensively without first
    having to re-verify membership themselves.
    """
    try:
        parent.children.remove(item)
    except ValueError:
        pass


def _file_matches(store_file: StoreFile, keyword: str) -> bool:
    """Case-insensitive substring match of `keyword` against a file's own
    name or its extracted content.

    Judgment call, documented here since the spec's match definition is
    worded specifically in terms of "the file's name" / "its extracted
    content": only `StoreFile` instances are ever considered a "match" by
    this sieve - a folder is never itself a match by virtue of its own name
    containing `keyword`; it only survives `sieve()` by transitively
    containing a matching file. This is the more literal reading of the
    spec's match definition, but it is a real, spottable judgment call (a
    human skimming a folder named e.g. "Invoices" typing "Invoices" into the
    keyword bar might reasonably expect that folder to surface on name alone
    even if empty/no matching children) - flagged here for anyone who wants
    to revisit it.

    Swallows any read failure (e.g. the file's source was deleted on disk
    since being added to the store) and treats it as "no content match"
    rather than raising, so one bad file never blanks the whole sieve.
    """
    lowered = keyword.lower()
    if lowered in store_file.name.lower():
        return True
    try:
        text = content_read.read_content(store_file.path)
    except Exception:
        return False
    return lowered in text.lower()


def sieve(root: StoreFolder, keyword: str) -> StoreFolder:
    """Return a PRUNED COPY of `root`: only folders that (transitively) lead
    to at least one matching file survive, as brand-new `StoreFolder`
    instances (the original tree is never mutated by searching) - plus the
    matching `StoreFile` leaves themselves (the same actual instances, since
    a leaf has no children to prune and there is nothing unsafe about
    referencing it directly from the pruned copy).

    A match is a case-insensitive substring match of `keyword` against a
    file's own name or its extracted content (see `_file_matches()`).
    Arrival order is preserved throughout.

    An empty/whitespace-only `keyword` is expected to never actually reach
    this function in normal use (the app layer only shows the top/pruned
    pane when the keyword bar is non-empty), but is handled defensively
    anyway: returns an empty pruned root rather than raising or matching
    everything.
    """
    pruned = StoreFolder(name=root.name)
    if not keyword or not keyword.strip():
        return pruned

    for child in root.children:
        if isinstance(child, StoreFolder):
            pruned_child = sieve(child, keyword)
            if pruned_child.children:
                pruned.children.append(pruned_child)
        elif isinstance(child, StoreFile):
            if _file_matches(child, keyword):
                pruned.children.append(child)

    return pruned
