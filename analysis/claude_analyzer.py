"""Core analysis engine using Claude API."""

import base64
import json
import os

import anthropic

import config
from analysis.scoring_criteria import load_criteria, calculate_category_score
from parsing.docx_parser import get_full_text


def _build_pdf_content_blocks(pdf_paths, log_fn=None):
    """Convert PDF paths into Anthropic document content blocks for multimodal input.

    Returns a list of content blocks. PDFs that can't be read are skipped (logged).
    """
    blocks = []
    for path in (pdf_paths or []):
        try:
            with open(path, "rb") as f:
                data = base64.b64encode(f.read()).decode("utf-8")
            blocks.append({
                "type": "document",
                "source": {
                    "type": "base64",
                    "media_type": "application/pdf",
                    "data": data,
                },
            })
            if log_fn:
                size_kb = os.path.getsize(path) / 1024
                log_fn(f"Attached PDF for direct analysis: {os.path.basename(path)} ({size_kb:.0f} KB)")
        except Exception as e:
            if log_fn:
                log_fn(f"Could not attach PDF {os.path.basename(path)}: {e}")
    return blocks


def load_prompts():
    """Load admin-configurable prompts, falling back to packaged defaults for any
    missing keys. This lets new prompts (added in code) take effect on existing
    installs whose prompts.json predates them."""
    import os

    defaults_path = os.path.join(os.path.dirname(config.ADMIN_PROMPTS_PATH), "prompts_default.json")
    try:
        with open(defaults_path, "r") as f:
            merged = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        merged = {}

    try:
        with open(config.ADMIN_PROMPTS_PATH, "r") as f:
            user_prompts = json.load(f)
        # Admin-saved values override defaults, but only when non-empty so that
        # cleared fields in the admin UI don't blank-out a working default.
        for k, v in user_prompts.items():
            if v:
                merged[k] = v
    except (FileNotFoundError, json.JSONDecodeError):
        pass

    return merged


def analyze_contract(contract_text, ideal_template_text, statutes_context="", jurisdiction=None, model=None, log_fn=None, pdf_attachments=None):
    """Analyze a contract against the ideal template and scoring criteria.

    Args:
        contract_text: full text of the contract to review (may be empty if PDF
            text extraction failed — in that case pdf_attachments carries the
            authoritative content)
        ideal_template_text: full text of the ideal template agreement
        statutes_context: formatted string of relevant statutes
        jurisdiction: dict with state info from jurisdiction detector
        model: Claude model ID to use (overrides config default)
        log_fn: optional callback function(message) for progress logging
        pdf_attachments: optional list of paths to PDF files. When provided, the
            PDFs are sent to Claude as multimodal document content (handles
            scanned/OCR'd PDFs that pdfplumber can't extract).

    Returns:
        dict with analysis results including scores, explanations, and revisions
    """
    def log(msg):
        if log_fn:
            log_fn(msg)

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

    # Build contract section. If PDFs are attached, point the LLM at them as
    # the authoritative source. Extracted text (if any) is still included since
    # it helps with exact quoting in the response.
    pdf_blocks = _build_pdf_content_blocks(pdf_attachments, log_fn=log)
    if pdf_blocks:
        try:
            log(f"anthropic SDK version: {anthropic.__version__}")
        except Exception:
            pass
        if contract_text.strip():
            contract_section = (
                "The contract is provided as attached PDF document(s) — use those "
                "as the authoritative source. A best-effort text extraction is also "
                "included below to help with exact quoting:\n\n" + contract_text
            )
        else:
            contract_section = (
                "The contract is provided as attached PDF document(s). Direct text "
                "extraction failed (likely a scanned or non-standard PDF), so read "
                "the attached PDF(s) carefully as the authoritative source."
            )
    else:
        contract_section = contract_text

    user_prompt = f"""{analysis_prompt_template}

## CONTRACT UNDER REVIEW
{contract_section}

## IDEAL TEMPLATE AGREEMENT
{ideal_template_text}

## SCORING CRITERIA
{criteria_text}

## JURISDICTION INFORMATION
{jurisdiction_text}

## RELEVANT STATUTES
{statutes_context if statutes_context else "No cached statutes available for this jurisdiction."}

Please analyze the contract and return your complete analysis as JSON."""

    # Calculate word counts for logging
    contract_words = len(contract_text.split())
    template_words = len(ideal_template_text.split())
    total_prompt_words = len(user_prompt.split())
    num_criteria = sum(len(c["criteria"]) for c in criteria_data["categories"].values())

    log(f"Preparing analysis prompt: {contract_words:,} contract words + {template_words:,} template words")
    if pdf_blocks:
        log(f"Sending {len(pdf_blocks)} PDF(s) directly to {model} for multimodal analysis")
    log(f"Scoring against {num_criteria} criteria across {len(criteria_data['categories'])} categories")
    log(f"Sending {total_prompt_words:,} words to {model} (this may take 2-5 minutes)...")

    client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
    message_content = pdf_blocks + [{"type": "text", "text": user_prompt}] if pdf_blocks else user_prompt
    response = client.messages.create(
        model=model,
        max_tokens=config.MAX_TOKENS,
        system=system_prompt,
        messages=[{"role": "user", "content": message_content}],
    )

    response_text = response.content[0].text
    log(f"AI response received — {response.usage.output_tokens:,} tokens ({len(response_text):,} chars)")
    log("Parsing analysis results and computing scores...")

    analysis = _parse_analysis_response(response_text, criteria_data)

    if analysis.get("parse_error"):
        log("WARNING: Could not parse JSON from AI response — results may be incomplete")
    elif analysis.get("truncated_response"):
        score = analysis.get("overall_score", 0)
        num_cats = len(analysis.get("categories", {}))
        log(f"WARNING: AI response hit max output tokens — recovered {num_cats} categories with overall score {score:.0%}. Consider re-running for complete coverage.")
    else:
        score = analysis.get("overall_score", 0)
        log(f"Analysis parsed successfully — overall score: {score:.0%}")

    # Track token usage
    usage = {
        "analysis_input_tokens": response.usage.input_tokens,
        "analysis_output_tokens": response.usage.output_tokens,
        "model": model,
    }

    # Now get specific revision suggestions
    weak_count = 0
    for cat_data in analysis.get("categories", {}).values():
        for crit_result in cat_data.get("criteria", {}).values():
            if isinstance(crit_result, dict) and crit_result.get("score", 2) < 2:
                weak_count += 1

    if weak_count > 0:
        log(f"Found {weak_count} criteria scoring below threshold — requesting revision suggestions...")
    else:
        log("All criteria scored well — skipping revision request")

    revisions, rev_usage = _get_revisions(
        client, system_prompt, prompts, contract_text, analysis, model,
        log_fn=log_fn, pdf_attachments=pdf_attachments,
    )
    analysis["revisions"] = revisions

    usage["revision_input_tokens"] = rev_usage.get("input_tokens", 0)
    usage["revision_output_tokens"] = rev_usage.get("output_tokens", 0)
    usage["total_input_tokens"] = usage["analysis_input_tokens"] + usage["revision_input_tokens"]
    usage["total_output_tokens"] = usage["analysis_output_tokens"] + usage["revision_output_tokens"]
    usage["estimated_cost"] = _estimate_cost(model, usage["total_input_tokens"], usage["total_output_tokens"])
    analysis["usage"] = usage

    log(f"Total API usage: {usage['total_input_tokens']:,} input + {usage['total_output_tokens']:,} output tokens")
    log(f"Estimated cost: ${usage['estimated_cost']:.4f}")

    return analysis


def _get_revisions(client, system_prompt, prompts, contract_text, analysis, model=None, log_fn=None, pdf_attachments=None):
    """Get specific text revisions for track changes."""
    def log(msg):
        if log_fn:
            log_fn(msg)

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

    pdf_blocks = _build_pdf_content_blocks(pdf_attachments)
    if pdf_blocks:
        contract_section = (
            "The contract is in the attached PDF document(s) — use those as the "
            "authoritative source for exact verbatim quoting. Text extraction is "
            "also included below:\n\n" + (contract_text or "(text extraction was empty)")
        )
    else:
        contract_section = contract_text

    user_prompt = f"""{revision_prompt}

## CONTRACT TEXT
{contract_section}

## AREAS NEEDING REVISION
{json.dumps(weak_areas, indent=2)}

Generate specific, actionable text revisions as a JSON array."""

    log(f"Sending revision request to {model} for {len(weak_areas)} weak areas...")

    message_content = pdf_blocks + [{"type": "text", "text": user_prompt}] if pdf_blocks else user_prompt
    response = client.messages.create(
        model=model,
        max_tokens=config.MAX_TOKENS,
        system=system_prompt,
        messages=[{"role": "user", "content": message_content}],
    )

    revisions = _extract_json_array(response.content[0].text)
    log(f"Revision response received — {len(revisions)} specific text changes suggested")

    rev_usage = {
        "input_tokens": response.usage.input_tokens,
        "output_tokens": response.usage.output_tokens,
    }
    return revisions, rev_usage


def _estimate_cost(model, input_tokens, output_tokens):
    """Estimate API cost in USD based on model and token counts."""
    # Pricing per million tokens (as of 2025)
    pricing = {
        "claude-sonnet-4-6": {"input": 3.00, "output": 15.00},
        "claude-opus-4-7": {"input": 15.00, "output": 75.00},
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
    """Parse Claude's analysis response into structured results.

    Even if the response was truncated mid-JSON (e.g. hit max_tokens), the
    repair logic in _extract_json_object often recovers a partial categories
    map. We still want to compute scores from whatever was salvaged so the
    results page shows something meaningful instead of 0%.
    """
    result = _extract_json_object(response_text)
    if result and result.get("categories"):
        _compute_scores(result, criteria_data)
        # If repair was used (response was truncated), flag it so the UI can
        # warn the user that some criteria may be missing.
        if not response_text.rstrip().endswith("}"):
            result["truncated_response"] = True
        return result

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
