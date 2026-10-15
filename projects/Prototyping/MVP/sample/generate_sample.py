"""One-off script to generate sample/sample_input.docx.

Run once via the venv:
    .venv/Scripts/python.exe sample/generate_sample.py

Not part of the app; kept for reference / regeneration if needed.
"""

from pathlib import Path

from docx import Document

PARAGRAPHS = [
    "This is the first paragraph of the sample document.",
    "It contains a second paragraph with some different text, used to verify verbatim extraction.",
    "Finally, this third paragraph closes out the sample content.",
]


def main():
    document = Document()
    for paragraph_text in PARAGRAPHS:
        document.add_paragraph(paragraph_text)

    output_path = Path(__file__).parent / "sample_input.docx"
    document.save(str(output_path))
    print(f"Wrote {output_path}")


if __name__ == "__main__":
    main()
