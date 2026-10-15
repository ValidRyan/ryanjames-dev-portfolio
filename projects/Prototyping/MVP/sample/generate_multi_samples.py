"""One-off script to generate the multi-file sample set:
    sample/password_list.docx
    sample/equations.docx
    sample/wildcard.docx
    sample/love_letter.docx

Run once via the venv:
    .venv/Scripts/python.exe sample/generate_multi_samples.py

Not part of the app; kept for reference / regeneration if needed.
"""

from pathlib import Path

from docx import Document

PASSWORD_LIST_PARAGRAPHS = [
    "Totally Secure Password Vault (do not actually use any of these)",
    "Hunter2",
    "P@ssw0rd!2024",
    "correct-horse-battery-staple",
    "tr0ub4dor&3",
    "CoRrEcT_HoRsE_92!!",
    "1234567890!@#$%^&*()",
    "qwerty!!!ULTRA",
    "IAmTheAdmin$$$",
    "sunshine_rainbows_&_h4ck3rs",
]

# A deliberate mix: plain arithmetic (which `calculate` genuinely computes)
# alongside algebraic lines with variables or a literal "=" (which correctly
# fail to parse as pure arithmetic and pass through unchanged instead).
EQUATIONS_PARAGRAPHS = [
    "3 + 4",
    "12 * 8",
    "100 / 4",
    "2 ** 10",
    "x + 5 = 12",
    "7 - 2 * 3",
    "y = 2x + 3",
    "(5 + 3) * 2",
    "a^2 + b^2 = c^2",
    "9 % 4",
]

WILDCARD_PARAGRAPHS = [
    "cherry blossoms fall",
    "in the space between two frames",
    "silence, then color",
    "量子力学は美しい",
    "42 * 42",
    "a(==)b(=)c",
]

# Composed entirely from this project's own formal vocabulary rather than
# ordinary natural-language sentences - a(==)b(==)c, Input/Midput/Output,
# Feed -> Churn -> Receive, and the Knowledge = Phrase x Indata diagonal
# formula, closing on the product's own name/ellipsis.
LOVE_LETTER_PARAGRAPHS = [
    "a(==)b(==)c",
    "Input: this",
    "Midput: (held, without translation)",
    "Output: this, unreduced",
    "1 -> 1 -> 1 -> ...",
    "Feed -> Churn -> Receive -> Feed -> Churn -> Receive -> Feed -> ...",
    "Knowledge = Phrase x Indata",
    "Understanding = Phrase x Outdata",
    "(Goals, Limits: undefined - no boundary)",
    "...",
    "Go...",
]


def _write_docx(paragraphs, filename):
    document = Document()
    for paragraph_text in paragraphs:
        document.add_paragraph(paragraph_text)
    output_path = Path(__file__).parent / filename
    document.save(str(output_path))
    print(f"Wrote {output_path}")


def main():
    _write_docx(PASSWORD_LIST_PARAGRAPHS, "password_list.docx")
    _write_docx(EQUATIONS_PARAGRAPHS, "equations.docx")
    _write_docx(WILDCARD_PARAGRAPHS, "wildcard.docx")
    _write_docx(LOVE_LETTER_PARAGRAPHS, "love_letter.docx")


if __name__ == "__main__":
    main()
