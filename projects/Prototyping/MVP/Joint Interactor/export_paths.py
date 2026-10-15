"""Non-destructive export-path derivation, plus a same-path guard.

Every write this prototype ever performs must land on a node's own
`export_path`, never on an operator's own original source file - including
files under `Prototyping/MVP/sample/`, which must never be modified by
anything in Joint Interactor. `assert_not_original()` is the load-bearing safety net
that is meant to be called before every single export write, not a
formality.
"""

from pathlib import Path
from typing import Iterable, Union

import docx_write

PathLike = Union[str, Path]


def derive_default_export_path(source_path: PathLike) -> Path:
    """Same folder, same extension as source, distinguished by an `.export`
    infix - e.g. sample_input.docx -> sample_input.export.docx,
    sample_input.txt -> sample_input.export.txt. Symmetric across node kinds
    - depends only on the source path's own extension, never on node kind.
    Never collides with the node's own source by construction
    (stem.export.ext != stem.ext).
    """
    path = Path(source_path)
    return path.with_name(f"{path.stem}.export{path.suffix}")


def write_formatted(text: str, path: PathLike) -> None:
    """Write `text` to `path`, dispatching purely on `path`'s own extension
    (case-insensitive), never on node kind: a `.docx` target goes through
    `docx_write.write_docx()`; anything else is written as plain UTF-8 text.
    """
    path = Path(path)
    if path.suffix.lower() == ".docx":
        docx_write.write_docx(text, str(path))
    else:
        path.write_text(text, encoding="utf-8")


def assert_not_original(target: PathLike, known_originals: Iterable[PathLike]) -> None:
    """Raise if `target` would resolve to any operator's own original source path.

    Defense in depth - called before every export write, even though
    `derive_default_export_path()` is already designed to avoid this by
    construction. `known_originals` is typically the set of every operator
    node's `source_path` currently on the canvas. Scoped only to protecting
    original source files - it does NOT also guard against two different
    nodes' export targets colliding with each other; that residual case is
    an accepted simplification for this MVP, not a gap to close.
    """
    target_resolved = Path(target).resolve()
    for original in known_originals:
        if target_resolved == Path(original).resolve():
            raise ValueError(
                f"Refusing to write to {target_resolved}: it matches an "
                "operator's original source file, not a safe export target."
            )
