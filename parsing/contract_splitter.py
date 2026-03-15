"""Split contract text into canonical sections for analysis."""

import re

# Canonical section names mapped to common heading variations
SECTION_PATTERNS = {
    "Term": [
        r"term\s+of\s+agreement", r"\bterm\b", r"duration",
    ],
    "Compensation": [
        r"compensation", r"management\s+fee", r"fees?\b", r"schedule\s+of.*charges",
    ],
    "Duties of Manager": [
        r"duties\s+of\s+manager", r"services\s+of\s+agent", r"management\s+services",
        r"duties\s+of\s+agent",
    ],
    "Physical Management": [
        r"physical\s+management", r"site\s+reviews?", r"common\s+(area|element)\s+maintenance",
    ],
    "Vendor Management": [
        r"vendor\s+management", r"bids?\s+(and|&)\s+proposals?", r"project\s+coordination",
        r"vendor\s+insurance",
    ],
    "Administrative Management": [
        r"administrative\s+management", r"general\s+administration",
    ],
    "Meetings": [
        r"\bmeetings?\b",
    ],
    "Records": [
        r"records?\b", r"association\s+records",
    ],
    "Insurance": [
        r"\binsurance\b",
    ],
    "Fiscal Management": [
        r"fiscal\s+management", r"financial\s+management",
    ],
    "Assessment Collections": [
        r"assessment\s+collect", r"collections?\b",
    ],
    "Budget": [
        r"budget\b", r"budget\s+preparation",
    ],
    "Financial Statements": [
        r"financial\s+statements?",
    ],
    "Bank Accounts": [
        r"bank\s+accounts?", r"association\s+accounts?",
    ],
    "Expenses": [
        r"association\s+expenses", r"payment\s+of\s+expenses", r"\bexpenses\b",
    ],
    "Relationship to Association": [
        r"relationship.*manager.*association", r"relationship.*agent.*association",
        r"independent\s+contractor",
    ],
    "Relationship to Owners": [
        r"relationship.*manager.*owner", r"relationship.*agent.*owner",
        r"individual\s+owners",
    ],
    "Disputes": [
        r"disputes?\s+(and|&)\s+differences", r"owner.*disputes?", r"mediat",
    ],
    "Security": [
        r"\bsecurity\b",
    ],
    "Onboarding Offboarding": [
        r"onboarding", r"offboarding", r"community\s+onboarding",
    ],
    "Board Responsibilities": [
        r"board\s+responsibilit", r"board\s+member\s+dut", r"board\s+conduct",
        r"association.s\s+obligations",
    ],
    "Termination": [
        r"\btermination\b",
    ],
    "Assignment": [
        r"\bassignment\b", r"assignment\s+of\s+rights",
    ],
    "Indemnification": [
        r"indemnif", r"hold\s+harmless",
    ],
    "Liability": [
        r"liabilit", r"manager\s+assumes\s+no\s+liability",
    ],
    "Non-Compete": [
        r"non[- ]?compet", r"non[- ]?interference", r"non[- ]?solicitation",
    ],
    "Force Majeure": [
        r"force\s+majeure",
    ],
    "Notices": [
        r"\bnotices?\b",
    ],
    "Confidentiality": [
        r"confidentialit",
    ],
    "Complete Agreement": [
        r"complete\s+agreement", r"entire\s+agreement",
    ],
    "Applicable Law": [
        r"applicable\s+law", r"governing\s+law", r"laws?\s+of\s+the\s+state",
    ],
    "Right to Cure": [
        r"right\s+to\s+cure",
    ],
    "Waiver": [
        r"\bwaiver\b", r"no\s+waiver",
    ],
}


def split_into_sections(paragraphs):
    """Split parsed paragraphs into canonical sections.

    Args:
        paragraphs: list of dicts with 'style' and 'text' keys

    Returns:
        dict mapping section name to list of paragraph texts
    """
    sections = {}
    current_section = "Preamble"
    sections[current_section] = []

    for para in paragraphs:
        text = para["text"]
        # Check if this paragraph is a section heading
        matched_section = _match_section(text)
        if matched_section:
            current_section = matched_section
            if current_section not in sections:
                sections[current_section] = []
        sections.setdefault(current_section, []).append(text)

    return sections


def _match_section(text):
    """Check if text matches a known section heading."""
    # Only match relatively short text as headings
    if len(text) > 200:
        return None

    text_lower = text.lower().strip()
    # Remove section numbering
    text_lower = re.sub(r"^(section\s+)?\d+[\.\d]*\s*", "", text_lower)

    for section_name, patterns in SECTION_PATTERNS.items():
        for pattern in patterns:
            if re.search(pattern, text_lower, re.IGNORECASE):
                return section_name
    return None


def get_sections_text(sections):
    """Convert sections dict to a dict of section_name -> joined text."""
    return {name: "\n".join(paras) for name, paras in sections.items()}
