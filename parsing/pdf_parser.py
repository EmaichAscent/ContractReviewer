"""Parse PDF files using pdfplumber."""

import pdfplumber


def parse_pdf(filepath):
    """Extract text from a PDF file.

    Returns a list of dicts with keys: style, text, index
    """
    paragraphs = []
    with pdfplumber.open(filepath) as pdf:
        for page_num, page in enumerate(pdf.pages):
            text = page.extract_text()
            if text:
                for i, line in enumerate(text.split('\n')):
                    line = line.strip()
                    if line:
                        paragraphs.append({
                            "style": "Normal",
                            "text": line,
                            "index": f"page-{page_num + 1}-line-{i}",
                        })
    return paragraphs


def get_full_text_pdf(filepath):
    """Get full text from a PDF file."""
    paragraphs = parse_pdf(filepath)
    return "\n".join(p["text"] for p in paragraphs)
