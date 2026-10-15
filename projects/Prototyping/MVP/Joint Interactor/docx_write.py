"""Pure text-to-.docx writing logic - the reverse of docx_extract.extract_text().

Kept free of any UI dependencies, mirroring docx_extract.py's own framing
(File Recompiler only ever went docx -> txt; Joint Interactor needs the reverse direction
too, for Txt operator nodes exporting a Bi/live sibling back out as .docx).
"""

from docx import Document


def write_docx(text: str, path: str) -> None:
    """Write `text` to a .docx file at `path`, one paragraph per line.

    The reverse of `docx_extract.extract_text()`, which joins every
    paragraph's text with "\\n". Round-trip property:
    `extract_text(write_docx(t)) == t` for any `t` this pair round-trips
    through paragraph-per-line (no tables, no rich formatting - matches
    docx_extract's own "tables are explicitly out of scope" framing).
    """
    document = Document()
    for line in text.split("\n"):
        document.add_paragraph(line)
    document.save(str(path))
