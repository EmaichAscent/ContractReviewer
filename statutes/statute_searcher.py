"""Search the web for relevant HOA/condo statutes and cache results."""

import json
import anthropic

import config
from statutes.statute_db import get_statutes_for_state, save_statutes


# Known statute databases by state (common HOA/condo statutes)
KNOWN_STATUTES = {
    "FL": {
        "searches": [
            "Florida Chapter 718 Condominium Act full text",
            "Florida Chapter 719 Cooperative Act",
            "Florida Chapter 720 Homeowners Association Act",
            "Florida Statute 468 Community Association Manager licensing",
            "Florida Chapter 617 Not For Profit Corporations Act",
        ],
        "key_statutes": [
            {"statute_number": "Chapter 718", "title": "Florida Condominium Act"},
            {"statute_number": "Chapter 719", "title": "Florida Cooperative Act"},
            {"statute_number": "Chapter 720", "title": "Florida HOA Act"},
            {"statute_number": "Section 468", "title": "Community Association Management"},
            {"statute_number": "Chapter 617", "title": "Not For Profit Corporations"},
        ]
    },
    "NC": {
        "searches": [
            "North Carolina Chapter 47C Condominium Act",
            "North Carolina Chapter 47F Planned Community Act HOA",
            "North Carolina General Statutes HOA management",
        ],
        "key_statutes": [
            {"statute_number": "Chapter 47C", "title": "NC Condominium Act"},
            {"statute_number": "Chapter 47F", "title": "NC Planned Community Act"},
        ]
    },
}


def search_statutes_for_state(state_abbrev, contract_text=""):
    """Search for relevant statutes for a given state.

    First checks the cache. If not found or stale, uses Claude to identify
    relevant statutes and then searches the web to find them.

    Returns list of statute dicts.
    """
    # Check cache first
    cached = get_statutes_for_state(state_abbrev)
    if cached:
        return cached

    # Use Claude to identify and summarize relevant statutes
    statutes = _search_with_claude(state_abbrev, contract_text)

    # Cache the results
    if statutes:
        save_statutes(state_abbrev, statutes)

    return statutes


def _search_with_claude(state_abbrev, contract_text):
    """Use Claude with web search to find relevant statutes."""
    prompts_data = _load_prompts()
    state_name = _abbrev_to_name(state_abbrev)

    # Build search context from known statutes
    known = KNOWN_STATUTES.get(state_abbrev, {})
    known_context = ""
    if known.get("key_statutes"):
        known_context = "\n\nKnown key statutes for this state:\n"
        for s in known["key_statutes"]:
            known_context += f"- {s['statute_number']}: {s['title']}\n"

    prompt = prompts_data.get("statute_search_prompt", "").format(state=state_name)
    prompt += known_context
    if contract_text:
        # Include a summary of the contract for context (truncated)
        prompt += f"\n\nContract excerpt for context:\n{contract_text[:3000]}"

    try:
        client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
        response = client.messages.create(
            model=config.CLAUDE_MODEL,
            max_tokens=config.MAX_TOKENS,
            system="You are a legal research assistant specializing in HOA and condominium law. Provide accurate statute references with numbers, titles, and summaries. Return your response as a JSON array of objects with keys: statute_number, title, summary, relevance, source_url (use the official state legislature URL if known).",
            messages=[{"role": "user", "content": prompt}],
        )

        # Parse the response
        response_text = response.content[0].text
        # Try to extract JSON from the response
        statutes = _extract_json_array(response_text)
        return statutes

    except Exception as e:
        print(f"Error searching statutes with Claude: {e}")
        # Fall back to known statutes if available
        if known.get("key_statutes"):
            return [
                {
                    "statute_number": s["statute_number"],
                    "title": s["title"],
                    "summary": f"Key {state_name} statute governing community associations",
                    "relevance": "Primary governing statute",
                    "source_url": "",
                    "full_text": "",
                }
                for s in known["key_statutes"]
            ]
        return []


def _extract_json_array(text):
    """Extract a JSON array from text that may contain markdown or other content."""
    # Try direct parse first
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Try to find JSON array in the text
    import re
    match = re.search(r'\[.*\]', text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            pass

    return []


def _load_prompts():
    """Load prompts from the admin-configurable prompts file."""
    try:
        with open(config.ADMIN_PROMPTS_PATH, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _abbrev_to_name(abbrev):
    """Convert state abbreviation to full name."""
    from statutes.jurisdiction_detector import US_STATES
    for name, ab in US_STATES.items():
        if ab == abbrev.upper():
            return name.title()
    return abbrev
