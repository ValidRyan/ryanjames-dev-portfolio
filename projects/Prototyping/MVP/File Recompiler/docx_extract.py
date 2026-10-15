"""Pure text-extraction logic for .docx files.

Kept free of any UI dependencies so it is trivially unit-testable in
isolation from the PySide6 app.
"""

from docx import Document


def extract_text(docx_path: str) -> str:
    """Return the plain text content of a .docx file.

    Reads every paragraph in the document (via python-docx) and joins
    them with newlines, verbatim. Tables are explicitly out of scope
    for this pass and are not read.
    """
    document = Document(docx_path)
    return "\n".join(paragraph.text for paragraph in document.paragraphs)
