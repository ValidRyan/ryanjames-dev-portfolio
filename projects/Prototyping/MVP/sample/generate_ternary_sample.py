"""One-off script to generate sample/ternary_sample_input.docx.

Run once via the venv:
    .venv/Scripts/python.exe sample/generate_ternary_sample.py

Not part of the app; kept for reference / regeneration if needed.
"""

from pathlib import Path

from docx import Document

LONG_PARAGRAPH = (
    "This is an extensively long first paragraph, written specifically to give the "
    "midput editing box a substantial block of plain prose to carry through the "
    "equivalate instruction unchanged. It exists purely as filler content, with no "
    "calculable meaning of its own, so that the second paragraph's arithmetic can be "
    "tested alongside ordinary text in the same document, proving that the calculate "
    "instruction can tell the difference between a line of prose and a line of "
    "arithmetic without needing any special markup to do so."
)

CALCULATION_PARAGRAPH = "2*6"

PARAGRAPHS = [LONG_PARAGRAPH, CALCULATION_PARAGRAPH]


def main():
    document = Document()
    for paragraph_text in PARAGRAPHS:
        document.add_paragraph(paragraph_text)

    output_path = Path(__file__).parent / "ternary_sample_input.docx"
    document.save(str(output_path))
    print(f"Wrote {output_path}")


if __name__ == "__main__":
    main()
