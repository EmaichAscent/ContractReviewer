"""Core analysis engine using Claude API."""

import json
import anthropic

import config
from analysis.scoring_criteria import load_criteria, calculate_category_score
from parsing.docx_parser import get_full_text


def load_prompts():
    """Load admin-configurable prompts."""
    try:
        with open(config.ADMIN_PROMPTS_PATH, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def analyze_contract(contract_text, ideal_template_text, statutes_context="", jurisdiction=None, model=None):
    """Analyze a contract against the ideal template and scoring criteria.

    Args:
        contract_text: full text of the contract to review
        ideal_template_text: full text of the ideal template agreement
        statutes_context: formatted string of relevant statutes
        jurisdiction: dict with state info from jurisdiction detector
        model: Claude model ID to use (overrides config default)

    Returns:
        dict with analysis results including scores, explanations, and revisions
    """
    prompts = load_prompts()
    criteria_data = load_criteria()
    model = model or config.CLAUDE_MODEL

    system_prompt = prompts.get("system_prompt", "You are an expert contract reviewer.")
    analysis_prompt_template = prompts.get("analysis_prompt", "Analyze this contract.")

    # Build the full analysis prompt
    criteria_text = _format_criteria(criteria_data)
    jurisdiction_text = ""
    if jurisdiction:
        jurisdiction_text = f"\nJurisdiction: {jurisdiction.get('state', 'Unknown')} ({jurisdiction.get('state_abbrev', '')})"
        if jurisdiction.get("statutes_mentioned"):
            jurisdiction_text += f"\nStatutes referenced in contract: {', '.join(jurisdiction['statutes_mentioned'])}"

    user_prompt = f"""{analysis_prompt_template}

## CONTRACT UNDER REVIEW
{contract_text}

## IDEAL TEMPLATE AGREEMENT
{ideal_template_text}

## SCORING CRITERIA
{criteria_text}

## JURISDICTION INFORMATION
{jurisdiction_text}

## RELEVANT STATUTES
{statutes_context if statutes_context else "No cached statutes available for this jurisdiction."}

Please analyze the contract and return your complete analysis as JSON."""

    client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
    response = client.messages.create(
        model=model,
        max_tokens=config.MAX_TOKENS,
        system=system_prompt,
        messages=[{"role": "user", "content": user_prompt}],
    )

    response_text = response.content[0].text
    analysis = _parse_analysis_response(response_text, criteria_data)

    # Track token usage
    usage = {
        "analysis_input_tokens": response.usage.input_tokens,
        "analysis_output_tokens": response.usage.output_tokens,
        "model": model,
    }

    # Now get specific revision suggestions
    revisions, rev_usage = _get_revisions(
        client, system_prompt, prompts, contract_text, analysis, model
    )
    analysis["revisions"] = revisions

    usage["revision_input_tokens"] = rev_usage.get("input_tokens", 0)
    usage["revision_output_tokens"] = rev_usage.get("output_tokens", 0)
    usage["total_input_tokens"] = usage["analysis_input_tokens"] + usage["revision_input_tokens"]
    usage["total_output_tokens"] = usage["analysis_output_tokens"] + usage["revision_output_tokens"]
    usage["estimated_cost"] = _estimate_cost(model, usage["total_input_tokens"], usage["total_output_tokens"])
    analysis["usage"] = usage

    return analysis


def _get_revisions(client, system_prompt, prompts, contract_text, analysis, model=None):
    """Get specific text revisions for track changes."""
    model = model or config.CLAUDE_MODEL
    revision_prompt = prompts.get("revision_prompt", "Suggest revisions.")

    # Collect all criteria that scored below 2
    weak_areas = []
    for cat_name, cat_data in analysis.get("categories", {}).items():
        for crit_id, crit_result in cat_data.get("criteria", {}).items():
            if isinstance(crit_result, dict) and crit_result.get("score", 2) < 2:
                weak_areas.append({
                    "category": cat_name,
                    "criterion": crit_id,
                    "score": crit_result.get("score", 0),
                    "explanation": crit_result.get("explanation", ""),
                    "suggested_revision": crit_result.get("suggested_revision", ""),
                })

    if not weak_areas:
        return [], {"input_tokens": 0, "output_tokens": 0}

    user_prompt = f"""{revision_prompt}

## CONTRACT TEXT
{contract_text}

## AREAS NEEDING REVISION
{json.dumps(weak_areas, indent=2)}

Generate specific, actionable text revisions as a JSON array."""

    response = client.messages.create(
        model=model,
        max_tokens=config.MAX_TOKENS,
        system=system_prompt,
        messages=[{"role": "user", "content": user_prompt}],
    )

    rev_usage = {
        "input_tokens": response.usage.input_tokens,
        "output_tokens": response.usage.output_tokens,
    }
    return _extract_json_array(response.content[0].text), rev_usage


def _estimate_cost(model, input_tokens, output_tokens):
    """Estimate API cost in USD based on model and token counts."""
    # Pricing per million tokens (as of 2025)
    pricing = {
        "claude-sonnet-4-6": {"input": 3.00, "output": 15.00},
        "claude-opus-4-6": {"input": 15.00, "output": 75.00},
    }
    rates = pricing.get(model, pricing["claude-sonnet-4-6"])
    cost = (input_tokens * rates["input"] / 1_000_000) + (output_tokens * rates["output"] / 1_000_000)
    return round(cost, 4)


def _format_criteria(criteria_data):
    """Format scoring criteria as readable text for the prompt."""
    lines = []
    for cat_name, cat_info in criteria_data["categories"].items():
        lines.append(f"\n### {cat_name} (weight: {cat_info['weight']})")
        lines.append(cat_info["description"])
        for crit in cat_info["criteria"]:
            lines.append(f"  - {crit['id']}: {crit['name']}")
            lines.append(f"    Description: {crit['description']}")
            lines.append(f"    Ideal: {crit['ideal']}")
    return "\n".join(lines)


def _parse_analysis_response(response_text, criteria_data):
    """Parse Claude's analysis response into structured results."""
    # Try to extract JSON
    result = _extract_json_object(response_text)
    if result:
        # Calculate scores from parsed data
        _compute_scores(result, criteria_data)
        return result

    # If JSON parsing fails, return a basic structure with the raw response
    return {
        "categories": {},
        "overall_recommendation": response_text,
        "statute_concerns": [],
        "parse_error": True,
        "raw_response": response_text,
    }


def _compute_scores(analysis, criteria_data):
    """Compute category and overall scores from criterion-level scores."""
    category_scores = {}
    for cat_name, cat_data in analysis.get("categories", {}).items():
        criteria = cat_data.get("criteria", {})
        scores = {}
        for crit_id, crit_result in criteria.items():
            if isinstance(crit_result, dict) and "score" in crit_result:
                scores[crit_id] = crit_result["score"]
        cat_score = calculate_category_score(scores)
        cat_data["score"] = round(cat_score, 2)
        category_scores[cat_name] = cat_score

    # Overall weighted score
    from analysis.scoring_criteria import calculate_overall_score
    analysis["overall_score"] = round(calculate_overall_score(category_scores), 2)


def _extract_json_object(text):
    """Extract a JSON object from text, handling markdown code blocks and truncation."""
    import re

    # Strip markdown code block wrapper first
    cleaned = text.strip()
    if cleaned.startswith('```json'):
        cleaned = cleaned[7:]
    elif cleaned.startswith('```'):
        cleaned = cleaned[3:]
    if cleaned.endswith('```'):
        cleaned = cleaned[:-3]
    cleaned = cleaned.strip()

    # Direct parse
    try:
        return json.loads(cleaned)
    except (json.JSONDecodeError, ValueError):
        pass

    # Find the outermost { } using brace counting
    balanced = _find_balanced_json(cleaned, '{', '}')
    if balanced:
        try:
            return json.loads(balanced)
        except (json.JSONDecodeError, ValueError):
            pass

    # Handle truncated JSON - try to repair by closing open structures
    repaired = _repair_truncated_json(cleaned)
    if repaired:
        try:
            return json.loads(repaired)
        except (json.JSONDecodeError, ValueError):
            pass

    return None


def _extract_json_array(text):
    """Extract a JSON array from text, handling markdown code blocks."""
    import re

    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        pass

    match = re.search(r'```(?:json)?\s*\n?(.*?)\n?\s*```', text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(1))
        except (json.JSONDecodeError, ValueError):
            pass

    result = _find_balanced_json(text, '[', ']')
    if result:
        try:
            return json.loads(result)
        except (json.JSONDecodeError, ValueError):
            pass

    return []


def _repair_truncated_json(text):
    """Attempt to repair JSON that was truncated mid-stream.

    Strategy: progressively try closing the JSON from the end,
    working backwards to find the longest valid parse.
    """
    if not text or text[0] != '{':
        return None

    # Try adding closing characters to make it valid
    # Work from the end of the text backwards, trying to find a repair point
    for trim_pos in range(len(text), max(0, len(text) - 5000), -1):
        candidate = text[:trim_pos].rstrip()

        # Remove any trailing comma or colon
        while candidate and candidate[-1] in ',: ':
            candidate = candidate[:-1]

        # Count open/close braces and brackets
        depth_curly = 0
        depth_square = 0
        in_string = False
        escape_next = False

        for ch in candidate:
            if escape_next:
                escape_next = False
                continue
            if ch == '\\' and in_string:
                escape_next = True
                continue
            if ch == '"':
                in_string = not in_string
                continue
            if in_string:
                continue
            if ch == '{':
                depth_curly += 1
            elif ch == '}':
                depth_curly -= 1
            elif ch == '[':
                depth_square += 1
            elif ch == ']':
                depth_square -= 1

        # If we're inside a string, skip
        if in_string:
            continue

        # Try to close all open structures
        if depth_curly > 0 or depth_square > 0:
            closing = ']' * depth_square + '}' * depth_curly
            try:
                result = json.loads(candidate + closing)
                # Only accept if we got meaningful content
                if result.get('categories'):
                    return candidate + closing
            except (json.JSONDecodeError, ValueError):
                continue

    return None


def _find_balanced_json(text, open_char, close_char):
    """Find the outermost balanced JSON structure using brace counting."""
    start = text.find(open_char)
    if start == -1:
        return None

    depth = 0
    in_string = False
    escape_next = False

    for i in range(start, len(text)):
        ch = text[i]
        if escape_next:
            escape_next = False
            continue
        if ch == '\\' and in_string:
            escape_next = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == open_char:
            depth += 1
        elif ch == close_char:
            depth -= 1
            if depth == 0:
                return text[start:i + 1]

    return None
