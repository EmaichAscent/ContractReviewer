"""Build a Client Template Edition: a revised copy of the firm's master template
with track changes (insertions/deletions) and Word comments explaining each
edit's source and purpose.

This is a second, focused LLM call that runs lazily when the user clicks
"Build Client Template Edition" on the results page. It reuses the contract
analysis (categories, statute_concerns, gap_analysis) as context and asks
Claude to produce a list of template-targeted revisions with metadata.

Track changes use the same lxml-based XML approach as output/trackchanges.py.
Word comments are not supported by python-docx natively, so we add them by
creating word/comments.xml and the relationships/content-type wiring directly.
"""

import copy
import json
import os
import re
import shutil
import tempfile
import zipfile
from datetime import datetime

import anthropic
from lxml import etree

import config
from analysis.claude_analyzer import load_prompts
from parsing.docx_parser import get_full_text


WORD_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
XML_NS = "http://www.w3.org/XML/1998/namespace"
RELS_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
CT_NS = "http://schemas.openxmlformats.org/package/2006/content-types"
COMMENTS_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/comments"
COMMENTS_CT = "application/vnd.openxmlformats-officedocument.wordprocessingml.comments+xml"

AUTHOR = "CAM Leadership Review"
INITIALS = "CLR"


def generate_client_template_edition(
    template_path,
    output_path,
    client_name,
    analysis,
    jurisdiction=None,
    model=None,
):
    """Generate a Client Template Edition of the master template.

    Args:
        template_path: path to the firm's master template .docx
        output_path: where to write the revised template
        client_name: client name (for header/branding)
        analysis: the analysis dict from analyze_contract
        jurisdiction: dict with state info (state, state_abbrev)
        model: Claude model ID; falls back to config.CLAUDE_MODEL
    """
    if not os.path.exists(template_path):
        raise FileNotFoundError(f"Template not found at {template_path}")

    model = model or config.CLAUDE_MODEL
    template_text = get_full_text(template_path)
    revisions, exec_summary = _request_template_revisions(
        template_text=template_text,
        analysis=analysis,
        client_name=client_name,
        jurisdiction=jurisdiction,
        model=model,
    )

    _apply_revisions_to_template(template_path, output_path, revisions, client_name, exec_summary)
    return {"revisions_applied": revisions, "executive_summary": exec_summary}


def _request_template_revisions(template_text, analysis, client_name, jurisdiction, model):
    """Call Claude to produce a structured list of template revisions."""
    prompts = load_prompts()
    system_prompt = prompts.get("template_revision_system_prompt") or prompts.get("system_prompt", "")
    user_template = prompts.get("template_revision_prompt", "")

    juris_line = ""
    if jurisdiction:
        juris_line = f"{jurisdiction.get('state', 'Unknown')} ({jurisdiction.get('state_abbrev', '')})"

    # Trim the analysis to what's needed for template revision: categories with
    # criterion explanations, statute_concerns, gap_analysis. Drop usage data,
    # raw responses, and verbose revisions arrays.
    analysis_context = {
        "categories": analysis.get("categories", {}),
        "statute_concerns": analysis.get("statute_concerns", []),
        "gap_analysis": analysis.get("gap_analysis", []),
        "overall_recommendation": analysis.get("overall_recommendation", ""),
    }

    user_prompt = f"""{user_template}

## CLIENT
{client_name}

## GOVERNING JURISDICTION
{juris_line}

## MASTER TEMPLATE (to be revised)
{template_text}

## CLIENT CONTRACT ANALYSIS (findings to draw from)
{json.dumps(analysis_context, indent=2, default=str)}

Return the JSON object as specified."""

    client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
    # Streaming is required when max_tokens + prompt size make the call
    # potentially exceed the SDK's 10-minute non-streaming ceiling.
    with client.messages.stream(
        model=model,
        max_tokens=config.MAX_TOKENS,
        system=system_prompt,
        messages=[{"role": "user", "content": user_prompt}],
    ) as stream:
        response = stream.get_final_message()

    parsed = _extract_json_object(response.content[0].text) or {}
    revisions = parsed.get("revisions", []) or []
    exec_summary = parsed.get("executive_summary", {}) or {}
    return revisions, exec_summary


def _apply_revisions_to_template(template_path, output_path, revisions, client_name, exec_summary):
    """Apply track-changed insertions/deletions plus Word comments to the template."""
    shutil.copy2(template_path, output_path)
    temp_dir = tempfile.mkdtemp()
    date_str = datetime.now().strftime("%Y-%m-%dT%H:%M:%SZ")

    try:
        with zipfile.ZipFile(output_path, "r") as z:
            z.extractall(temp_dir)

        doc_xml_path = os.path.join(temp_dir, "word", "document.xml")
        doc_tree = etree.parse(doc_xml_path)
        doc_root = doc_tree.getroot()

        comments = []  # list of {id, author, initials, date, text}
        rev_id = 200
        comment_id = 1
        applied = 0

        for rev in revisions:
            rtype = (rev.get("type") or "").lower()
            anchor = (rev.get("anchor_text") or "").strip()
            new_text = (rev.get("new_text") or "").strip()
            comment_text = _format_comment_body(rev)

            if rtype == "insert" and new_text:
                ok, rev_id = _apply_insert(
                    doc_root, anchor, new_text, rev_id, date_str,
                    comment_id=comment_id,
                )
            elif rtype == "replace" and anchor and new_text:
                ok, rev_id = _apply_replace(
                    doc_root, anchor, new_text, rev_id, date_str,
                    comment_id=comment_id,
                )
            elif rtype == "delete" and anchor:
                ok, rev_id = _apply_delete(
                    doc_root, anchor, rev_id, date_str,
                    comment_id=comment_id,
                )
            else:
                ok = False

            if ok:
                comments.append({
                    "id": comment_id,
                    "author": AUTHOR,
                    "initials": INITIALS,
                    "date": date_str,
                    "text": comment_text,
                })
                comment_id += 1
                applied += 1
            else:
                anchor_preview = anchor[:60] if anchor else "(no anchor)"
                print(f"  Template edition: FAILED to apply {rtype} — anchor: {anchor_preview}")

        print(f"Template edition: applied {applied}/{len(revisions)} revisions")

        # Prepend client branding header
        _insert_client_header(doc_root, client_name, exec_summary)

        # Enable track changes display and write comments part
        _enable_track_changes(temp_dir)
        if comments:
            _write_comments_part(temp_dir, comments)

        doc_tree.write(doc_xml_path, xml_declaration=True, encoding="UTF-8", standalone=True)
        _repackage_docx(temp_dir, output_path)

    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Track-change application (with embedded commentRangeStart/End + reference)
# ---------------------------------------------------------------------------


def _apply_insert(root, after_text, new_text, rev_id, date_str, comment_id=None):
    if after_text:
        anchor_para = _find_paragraph_containing(root, after_text)
    else:
        body = root.find(f"{{{WORD_NS}}}body")
        paras = body.findall(f"{{{WORD_NS}}}p") if body is not None else []
        anchor_para = paras[-1] if paras else None

    if anchor_para is None:
        return False, rev_id

    new_para = etree.Element(f"{{{WORD_NS}}}p")

    if comment_id is not None:
        crs = etree.SubElement(new_para, f"{{{WORD_NS}}}commentRangeStart")
        crs.set(f"{{{WORD_NS}}}id", str(comment_id))

    ins_elem = _make_element("ins", {
        f"{{{WORD_NS}}}id": str(rev_id),
        f"{{{WORD_NS}}}author": AUTHOR,
        f"{{{WORD_NS}}}date": date_str,
    })
    rev_id += 1

    new_run = etree.SubElement(ins_elem, f"{{{WORD_NS}}}r")
    new_t = etree.SubElement(new_run, f"{{{WORD_NS}}}t")
    new_t.set(f"{{{XML_NS}}}space", "preserve")
    new_t.text = new_text
    new_para.append(ins_elem)

    if comment_id is not None:
        cre = etree.SubElement(new_para, f"{{{WORD_NS}}}commentRangeEnd")
        cre.set(f"{{{WORD_NS}}}id", str(comment_id))
        cref_run = etree.SubElement(new_para, f"{{{WORD_NS}}}r")
        cref = etree.SubElement(cref_run, f"{{{WORD_NS}}}commentReference")
        cref.set(f"{{{WORD_NS}}}id", str(comment_id))

    anchor_para.addnext(new_para)
    return True, rev_id


def _apply_replace(root, original_text, new_text, rev_id, date_str, comment_id=None):
    para = _find_paragraph_containing(root, original_text)
    if para is None:
        return False, rev_id

    runs = list(para.findall(f"{{{WORD_NS}}}r"))
    if not runs:
        return False, rev_id

    first_rpr = None
    rpr = runs[0].find(f"{{{WORD_NS}}}rPr")
    if rpr is not None:
        first_rpr = copy.deepcopy(rpr)

    for run in runs:
        para.remove(run)

    if comment_id is not None:
        crs = etree.SubElement(para, f"{{{WORD_NS}}}commentRangeStart")
        crs.set(f"{{{WORD_NS}}}id", str(comment_id))

    del_elem = _make_element("del", {
        f"{{{WORD_NS}}}id": str(rev_id),
        f"{{{WORD_NS}}}author": AUTHOR,
        f"{{{WORD_NS}}}date": date_str,
    })
    rev_id += 1
    for run in runs:
        for t_elem in run.findall(f"{{{WORD_NS}}}t"):
            t_elem.tag = f"{{{WORD_NS}}}delText"
            t_elem.set(f"{{{XML_NS}}}space", "preserve")
        del_elem.append(run)
    para.append(del_elem)

    ins_elem = _make_element("ins", {
        f"{{{WORD_NS}}}id": str(rev_id),
        f"{{{WORD_NS}}}author": AUTHOR,
        f"{{{WORD_NS}}}date": date_str,
    })
    rev_id += 1
    new_run = etree.SubElement(ins_elem, f"{{{WORD_NS}}}r")
    if first_rpr is not None:
        new_run.append(first_rpr)
    new_t = etree.SubElement(new_run, f"{{{WORD_NS}}}t")
    new_t.set(f"{{{XML_NS}}}space", "preserve")
    new_t.text = new_text
    para.append(ins_elem)

    if comment_id is not None:
        cre = etree.SubElement(para, f"{{{WORD_NS}}}commentRangeEnd")
        cre.set(f"{{{WORD_NS}}}id", str(comment_id))
        cref_run = etree.SubElement(para, f"{{{WORD_NS}}}r")
        cref = etree.SubElement(cref_run, f"{{{WORD_NS}}}commentReference")
        cref.set(f"{{{WORD_NS}}}id", str(comment_id))

    return True, rev_id


def _apply_delete(root, text_to_delete, rev_id, date_str, comment_id=None):
    para = _find_paragraph_containing(root, text_to_delete)
    if para is None:
        return False, rev_id

    runs = list(para.findall(f"{{{WORD_NS}}}r"))
    if not runs:
        return False, rev_id

    for run in runs:
        para.remove(run)

    if comment_id is not None:
        crs = etree.SubElement(para, f"{{{WORD_NS}}}commentRangeStart")
        crs.set(f"{{{WORD_NS}}}id", str(comment_id))

    del_elem = _make_element("del", {
        f"{{{WORD_NS}}}id": str(rev_id),
        f"{{{WORD_NS}}}author": AUTHOR,
        f"{{{WORD_NS}}}date": date_str,
    })
    rev_id += 1
    for run in runs:
        for t_elem in run.findall(f"{{{WORD_NS}}}t"):
            t_elem.tag = f"{{{WORD_NS}}}delText"
            t_elem.set(f"{{{XML_NS}}}space", "preserve")
        del_elem.append(run)
    para.append(del_elem)

    if comment_id is not None:
        cre = etree.SubElement(para, f"{{{WORD_NS}}}commentRangeEnd")
        cre.set(f"{{{WORD_NS}}}id", str(comment_id))
        cref_run = etree.SubElement(para, f"{{{WORD_NS}}}r")
        cref = etree.SubElement(cref_run, f"{{{WORD_NS}}}commentReference")
        cref.set(f"{{{WORD_NS}}}id", str(comment_id))

    return True, rev_id


# ---------------------------------------------------------------------------
# Client header (prepend exec-summary preview to the document)
# ---------------------------------------------------------------------------


def _insert_client_header(root, client_name, exec_summary):
    body = root.find(f"{{{WORD_NS}}}body")
    if body is None:
        return
    sect = body.find(f"{{{WORD_NS}}}sectPr")
    first_child = body[0] if len(body) > 0 else None

    lines = [
        f"CAM Leadership Master Template — {client_name} Edition",
    ]
    if exec_summary:
        risk = exec_summary.get("overall_risk_rating")
        if risk:
            lines.append(f"Overall Risk Rating: {risk}")

    new_paras = []
    for i, text in enumerate(lines):
        p = etree.Element(f"{{{WORD_NS}}}p")
        r = etree.SubElement(p, f"{{{WORD_NS}}}r")
        rpr = etree.SubElement(r, f"{{{WORD_NS}}}rPr")
        etree.SubElement(rpr, f"{{{WORD_NS}}}b")
        t = etree.SubElement(r, f"{{{WORD_NS}}}t")
        t.set(f"{{{XML_NS}}}space", "preserve")
        t.text = text
        new_paras.append(p)

    if first_child is not None:
        for p in reversed(new_paras):
            first_child.addprevious(p)
    else:
        for p in new_paras:
            if sect is not None:
                sect.addprevious(p)
            else:
                body.append(p)


# ---------------------------------------------------------------------------
# Word comments part — direct XML manipulation
# ---------------------------------------------------------------------------


def _write_comments_part(temp_dir, comments):
    """Create word/comments.xml, register it in content types and document rels."""
    word_dir = os.path.join(temp_dir, "word")
    comments_path = os.path.join(word_dir, "comments.xml")

    nsmap = {"w": WORD_NS}
    root = etree.Element(f"{{{WORD_NS}}}comments", nsmap=nsmap)

    for c in comments:
        comment_el = etree.SubElement(root, f"{{{WORD_NS}}}comment")
        comment_el.set(f"{{{WORD_NS}}}id", str(c["id"]))
        comment_el.set(f"{{{WORD_NS}}}author", c["author"])
        comment_el.set(f"{{{WORD_NS}}}initials", c["initials"])
        comment_el.set(f"{{{WORD_NS}}}date", c["date"])

        # Split body into paragraphs (preserve line breaks as separate w:p elements)
        body_lines = (c["text"] or "").split("\n")
        for line in body_lines:
            p = etree.SubElement(comment_el, f"{{{WORD_NS}}}p")
            r = etree.SubElement(p, f"{{{WORD_NS}}}r")
            t = etree.SubElement(r, f"{{{WORD_NS}}}t")
            t.set(f"{{{XML_NS}}}space", "preserve")
            t.text = line

    etree.ElementTree(root).write(
        comments_path, xml_declaration=True, encoding="UTF-8", standalone=True
    )

    _register_comments_relationship(temp_dir)
    _register_comments_content_type(temp_dir)


def _register_comments_relationship(temp_dir):
    rels_path = os.path.join(temp_dir, "word", "_rels", "document.xml.rels")
    if not os.path.exists(rels_path):
        return

    tree = etree.parse(rels_path)
    root = tree.getroot()

    for rel in root.findall(f"{{{RELS_NS}}}Relationship"):
        if rel.get("Type") == COMMENTS_REL:
            return  # already present

    # Find max numeric Id to avoid collisions
    existing_ids = []
    for rel in root.findall(f"{{{RELS_NS}}}Relationship"):
        rid = rel.get("Id", "")
        m = re.match(r"rId(\d+)", rid)
        if m:
            existing_ids.append(int(m.group(1)))
    next_id = (max(existing_ids) + 1) if existing_ids else 1

    new_rel = etree.SubElement(root, f"{{{RELS_NS}}}Relationship")
    new_rel.set("Id", f"rId{next_id}")
    new_rel.set("Type", COMMENTS_REL)
    new_rel.set("Target", "comments.xml")

    tree.write(rels_path, xml_declaration=True, encoding="UTF-8", standalone=True)


def _register_comments_content_type(temp_dir):
    ct_path = os.path.join(temp_dir, "[Content_Types].xml")
    if not os.path.exists(ct_path):
        return

    tree = etree.parse(ct_path)
    root = tree.getroot()

    for ov in root.findall(f"{{{CT_NS}}}Override"):
        if ov.get("PartName") == "/word/comments.xml":
            return

    override = etree.SubElement(root, f"{{{CT_NS}}}Override")
    override.set("PartName", "/word/comments.xml")
    override.set("ContentType", COMMENTS_CT)

    tree.write(ct_path, xml_declaration=True, encoding="UTF-8", standalone=True)


# ---------------------------------------------------------------------------
# Helpers shared with output/trackchanges.py (intentionally duplicated to keep
# this module self-contained — it can be lifted into a shared util later)
# ---------------------------------------------------------------------------


def _format_comment_body(revision):
    """Build the Word comment text from a revision's metadata."""
    source_label = {
        "client_contract": "Client Contract",
        "statute": "State Statute",
        "recommendation": "CAM Leadership Recommendation",
    }.get((revision.get("source") or "").lower(), "CAM Leadership Recommendation")

    lines = [f"CAM Leadership Review Note"]
    lines.append(f"Source: {source_label}")
    purpose = revision.get("purpose")
    if purpose:
        lines.append(f"Purpose: {purpose}")
    statute_ref = revision.get("statute_ref")
    if statute_ref:
        lines.append(f"Statute Reference: {statute_ref}")
    client_quote = revision.get("client_quote")
    if client_quote:
        # Truncate long client quotes so the comment stays readable
        snippet = client_quote if len(client_quote) <= 350 else client_quote[:350] + "..."
        lines.append(f'Client Language: "{snippet}"')
    return "\n".join(lines)


def _find_paragraph_containing(root, search_text):
    if not search_text:
        return None
    paragraphs = root.findall(f".//{{{WORD_NS}}}p")
    for frag_len in [80, 60, 40, 25]:
        frag = search_text[:frag_len].strip()
        if not frag:
            continue
        for para in paragraphs:
            if frag in _get_paragraph_text(para):
                return para
        normalized = ' '.join(frag.split())
        if len(normalized) < 10:
            continue
        for para in paragraphs:
            if normalized in ' '.join(_get_paragraph_text(para).split()):
                return para
    short = ' '.join(search_text[:40].split()).lower()
    if len(short) >= 15:
        for para in paragraphs:
            if short in ' '.join(_get_paragraph_text(para).split()).lower():
                return para
    return None


def _get_paragraph_text(para):
    texts = []
    for elem in para.iter():
        if elem.tag in (f"{{{WORD_NS}}}t", f"{{{WORD_NS}}}delText"):
            if elem.text:
                texts.append(elem.text)
    return "".join(texts)


def _make_element(local_name, attribs):
    elem = etree.Element(f"{{{WORD_NS}}}{local_name}")
    for k, v in attribs.items():
        elem.set(k, v)
    return elem


def _enable_track_changes(temp_dir):
    settings_path = os.path.join(temp_dir, "word", "settings.xml")
    if not os.path.exists(settings_path):
        return
    tree = etree.parse(settings_path)
    root = tree.getroot()
    for existing in root.findall(f"{{{WORD_NS}}}trackChanges"):
        root.remove(existing)
    etree.SubElement(root, f"{{{WORD_NS}}}trackChanges")
    tree.write(settings_path, xml_declaration=True, encoding="UTF-8", standalone=True)


def _repackage_docx(temp_dir, output_path):
    with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for dirpath, dirnames, filenames in os.walk(temp_dir):
            for filename in filenames:
                file_path = os.path.join(dirpath, filename)
                arc_name = os.path.relpath(file_path, temp_dir)
                zf.write(file_path, arc_name)


def _extract_json_object(text):
    """Best-effort extraction of a JSON object from LLM output (handles fences,
    trailing prose, and mild truncation)."""
    cleaned = (text or "").strip()
    if cleaned.startswith("```json"):
        cleaned = cleaned[7:]
    elif cleaned.startswith("```"):
        cleaned = cleaned[3:]
    if cleaned.endswith("```"):
        cleaned = cleaned[:-3]
    cleaned = cleaned.strip()

    try:
        return json.loads(cleaned)
    except (json.JSONDecodeError, ValueError):
        pass

    # Find outermost balanced { }
    start = cleaned.find("{")
    if start == -1:
        return None
    depth = 0
    in_string = False
    escape = False
    for i in range(start, len(cleaned)):
        ch = cleaned[i]
        if escape:
            escape = False
            continue
        if ch == "\\" and in_string:
            escape = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(cleaned[start:i + 1])
                except (json.JSONDecodeError, ValueError):
                    return None
    return None
