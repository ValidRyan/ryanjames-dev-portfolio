"""Pure formatting logic for combined multi-file exports.

Kept free of any UI dependencies so it is trivially unit-testable in
isolation from the PySide6 app, mirroring docx_extract.py's pattern.
"""

from pathlib import Path


def build_combined_text(named_results: list[tuple[str, str]]) -> str:
    """Join (source_path, result_text) pairs into one combined export.

    One labeled block per source file (using just the file's own name, not
    its full path), separated by a blank line.
    """
    blocks = [
        f"--- {Path(source_path).name} ---\n{result_text}"
        for source_path, result_text in named_results
    ]
    return "\n\n".join(blocks)
