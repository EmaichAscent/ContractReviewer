"""Detect jurisdiction (state) from contract text."""

import re

US_STATES = {
    "ALABAMA": "AL", "ALASKA": "AK", "ARIZONA": "AZ", "ARKANSAS": "AR",
    "CALIFORNIA": "CA", "COLORADO": "CO", "CONNECTICUT": "CT", "DELAWARE": "DE",
    "FLORIDA": "FL", "GEORGIA": "GA", "HAWAII": "HI", "IDAHO": "ID",
    "ILLINOIS": "IL", "INDIANA": "IN", "IOWA": "IA", "KANSAS": "KS",
    "KENTUCKY": "KY", "LOUISIANA": "LA", "MAINE": "ME", "MARYLAND": "MD",
    "MASSACHUSETTS": "MA", "MICHIGAN": "MI", "MINNESOTA": "MN", "MISSISSIPPI": "MS",
    "MISSOURI": "MO", "MONTANA": "MT", "NEBRASKA": "NE", "NEVADA": "NV",
    "NEW HAMPSHIRE": "NH", "NEW JERSEY": "NJ", "NEW MEXICO": "NM", "NEW YORK": "NY",
    "NORTH CAROLINA": "NC", "NORTH DAKOTA": "ND", "OHIO": "OH", "OKLAHOMA": "OK",
    "OREGON": "OR", "PENNSYLVANIA": "PA", "RHODE ISLAND": "RI",
    "SOUTH CAROLINA": "SC", "SOUTH DAKOTA": "SD", "TENNESSEE": "TN", "TEXAS": "TX",
    "UTAH": "UT", "VERMONT": "VT", "VIRGINIA": "VA", "WASHINGTON": "WA",
    "WEST VIRGINIA": "WV", "WISCONSIN": "WI", "WYOMING": "WY",
}

# Known HOA/condo statute patterns by state
STATE_STATUTE_PATTERNS = {
    "FL": [
        r"chapter\s+718", r"chapter\s+719", r"chapter\s+720",
        r"florida\s+statut", r"f\.?s\.?\s+\d+",
        r"statutes?\s+468",
    ],
    "NC": [
        r"chapter\s+47[cf]", r"n\.?c\.?\s+gen", r"north\s+carolina\s+general\s+statut",
    ],
    "TX": [
        r"texas\s+property\s+code", r"chapter\s+20[79]", r"chapter\s+82",
    ],
    "CA": [
        r"davis.stirling", r"civil\s+code\s+\u00a7?\s*4[0-9]{3}",
    ],
}


def detect_jurisdiction(contract_text):
    """Detect the state jurisdiction from contract text.

    Returns dict with:
        state: full state name
        state_abbrev: two-letter abbreviation
        statutes_mentioned: list of statute references found
        confidence: 'high', 'medium', or 'low'
    """
    text_upper = contract_text.upper()
    result = {
        "state": None,
        "state_abbrev": None,
        "statutes_mentioned": [],
        "confidence": "low",
    }

    # Pattern 1: "laws of the State of [STATE]"
    match = re.search(
        r"laws?\s+of\s+the\s+state\s+of\s+([A-Za-z\s]+?)(?:\.|,|\s+if|\s+in|\s+shall)",
        contract_text, re.IGNORECASE
    )
    if match:
        state_name = match.group(1).strip().upper()
        if state_name in US_STATES:
            result["state"] = state_name.title()
            result["state_abbrev"] = US_STATES[state_name]
            result["confidence"] = "high"

    # Pattern 2: "[STATE] statutes" or "licensed in accordance with [STATE]"
    if not result["state"]:
        for state_name, abbrev in US_STATES.items():
            patterns = [
                rf"{state_name}\s+statut",
                rf"licensed.*{state_name}",
                rf"state\s+of\s+{state_name}",
                rf"{state_name}\s+law",
                rf"{state_name}\s+corporation",
                rf"{state_name}\s+not.for.profit",
                rf"{state_name}\s+administrative\s+code",
                rf"county,?\s+{state_name}",
            ]
            for pat in patterns:
                if re.search(pat, text_upper, re.IGNORECASE):
                    result["state"] = state_name.title()
                    result["state_abbrev"] = abbrev
                    result["confidence"] = "high"
                    break
            if result["state"]:
                break

    # Pattern 3: Location/address containing state
    if not result["state"]:
        match = re.search(
            r"(?:location|address|located\s+(?:in|at))[\s:]+.*?([A-Z]{2})\s+\d{5}",
            contract_text, re.IGNORECASE
        )
        if match:
            abbrev = match.group(1).upper()
            for state_name, sa in US_STATES.items():
                if sa == abbrev:
                    result["state"] = state_name.title()
                    result["state_abbrev"] = abbrev
                    result["confidence"] = "medium"
                    break

    # Find specific statute references
    if result["state_abbrev"]:
        patterns = STATE_STATUTE_PATTERNS.get(result["state_abbrev"], [])
        for pat in patterns:
            matches = re.findall(pat, contract_text, re.IGNORECASE)
            result["statutes_mentioned"].extend(matches)

    # Also find generic statute references (chapters and statutes only, not internal sections)
    generic_matches = re.findall(
        r"(?:chapter|statute|§)\s*\d+[\.\d]*(?:\([a-z]\))?",
        contract_text, re.IGNORECASE
    )
    result["statutes_mentioned"].extend(generic_matches)
    result["statutes_mentioned"] = list(set(result["statutes_mentioned"]))

    return result
