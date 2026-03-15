"""Generate Word track changes (revision marks) by manipulating docx XML."""

import copy
import os
import re
import zipfile
import shutil
import tempfile
from datetime import datetime
from lxml import etree

WORD_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
XML_NS = "http://www.w3.org/XML/1998/namespace"

AUTHOR = "Contract Reviewer"


def apply_track_changes(input_path, output_path, revisions):
    """Apply track changes to a .docx file based on revision list."""
    if not revisions:
        shutil.copy2(input_path, output_path)
        return

    shutil.copy2(input_path, output_path)
    temp_dir = tempfile.mkdtemp()
    date_str = datetime.now().strftime("%Y-%m-%dT%H:%M:%SZ")

    try:
        with zipfile.ZipFile(output_path, "r") as z:
            z.extractall(temp_dir)

        doc_xml_path = os.path.join(temp_dir, "word", "document.xml")
        tree = etree.parse(doc_xml_path)
        root = tree.getroot()

        rev_id = 100
        applied = 0

        for revision in revisions:
            rev_type = revision.get("type", "replace")
            original_text = revision.get("original_text", "").strip()
            new_text = revision.get("new_text", "").strip()

            if not original_text and not new_text:
                continue

            # Clean up "INSERT AFTER:" prefix from old-style prompts
            if original_text.upper().startswith("INSERT AFTER:"):
                original_text = original_text[len("INSERT AFTER:"):].strip()
                # Strip leading section refs like "II.F - "
                import re
                original_text = re.sub(r'^[IVX]+\.[A-Z0-9]+\s*[-–—]\s*', '', original_text)
                # Strip surrounding quotes
                if original_text.startswith("'") and original_text.endswith("'"):
                    original_text = original_text[1:-1].strip()
                elif original_text.startswith('"') and original_text.endswith('"'):
                    original_text = original_text[1:-1].strip()
                rev_type = "insert"

            if rev_type == "replace" and original_text and new_text:
                success, rev_id = _apply_replace(root, original_text, new_text, rev_id, date_str)
                if success:
                    applied += 1
                else:
                    print(f"  Track changes: FAILED to match replace text: {original_text[:60]}...")
            elif rev_type == "insert" and new_text:
                success, rev_id = _apply_insert(root, original_text, new_text, rev_id, date_str)
                if success:
                    applied += 1
                else:
                    print(f"  Track changes: FAILED to match insert anchor: {original_text[:60]}...")
            elif rev_type == "delete" and original_text:
                success, rev_id = _apply_delete(root, original_text, rev_id, date_str)
                if success:
                    applied += 1
                else:
                    print(f"  Track changes: FAILED to match delete text: {original_text[:60]}...")

        print(f"Track changes: applied {applied}/{len(revisions)} revisions")

        _enable_track_changes(temp_dir)
        tree.write(doc_xml_path, xml_declaration=True, encoding="UTF-8", standalone=True)
        _repackage_docx(temp_dir, output_path)

    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def _find_paragraph_containing(root, search_text):
    """Find a paragraph element whose text contains the search text."""
    if not search_text:
        return None

    paragraphs = root.findall(f".//{{{WORD_NS}}}p")

    # Try multiple search fragment lengths for robustness
    for frag_len in [80, 60, 40, 25]:
        search_fragment = search_text[:frag_len].strip()
        if not search_fragment:
            continue

        # Exact match first
        for para in paragraphs:
            para_text = _get_paragraph_text(para)
            if search_fragment in para_text:
                return para

        # Normalized whitespace match (handles tabs, multiple spaces)
        normalized_search = ' '.join(search_fragment.split())
        if len(normalized_search) < 10:
            continue
        for para in paragraphs:
            para_text = ' '.join(_get_paragraph_text(para).split())
            if normalized_search in para_text:
                return para

    # Last resort: try case-insensitive matching with short fragment
    search_lower = ' '.join(search_text[:40].split()).lower()
    if len(search_lower) >= 15:
        for para in paragraphs:
            para_text = ' '.join(_get_paragraph_text(para).split()).lower()
            if search_lower in para_text:
                return para

    return None


def _apply_replace(root, original_text, new_text, rev_id, date_str):
    """Replace paragraph content with tracked deletion + insertion."""
    para = _find_paragraph_containing(root, original_text)
    if para is None:
        return False, rev_id

    # Collect existing runs
    runs = list(para.findall(f"{{{WORD_NS}}}r"))
    if not runs:
        return False, rev_id

    # Get formatting from first run
    first_rpr = None
    if runs:
        rpr = runs[0].find(f"{{{WORD_NS}}}rPr")
        if rpr is not None:
            first_rpr = copy.deepcopy(rpr)

    # Get paragraph properties
    ppr = para.find(f"{{{WORD_NS}}}pPr")

    # Clear paragraph of runs (keep pPr)
    for run in runs:
        para.remove(run)

    # Add deletion markup with original runs
    del_elem = _make_element("del", {
        f"{{{WORD_NS}}}id": str(rev_id),
        f"{{{WORD_NS}}}author": AUTHOR,
        f"{{{WORD_NS}}}date": date_str,
    })
    rev_id += 1

    for run in runs:
        # Convert w:t elements to w:delText
        for t_elem in run.findall(f"{{{WORD_NS}}}t"):
            t_elem.tag = f"{{{WORD_NS}}}delText"
            t_elem.set(f"{{{XML_NS}}}space", "preserve")
        del_elem.append(run)

    para.append(del_elem)

    # Add insertion markup with new text
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

    return True, rev_id


def _apply_insert(root, after_text, new_text, rev_id, date_str):
    """Insert a new paragraph with insertion markup."""
    if after_text:
        anchor_para = _find_paragraph_containing(root, after_text)
    else:
        # Insert at end of body
        body = root.find(f"{{{WORD_NS}}}body")
        paras = body.findall(f"{{{WORD_NS}}}p") if body is not None else []
        anchor_para = paras[-1] if paras else None

    if anchor_para is None:
        return False, rev_id

    # Create new paragraph
    new_para = etree.Element(f"{{{WORD_NS}}}p")

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
    anchor_para.addnext(new_para)

    return True, rev_id


def _apply_delete(root, text_to_delete, rev_id, date_str):
    """Mark a paragraph's content for deletion."""
    para = _find_paragraph_containing(root, text_to_delete)
    if para is None:
        return False, rev_id

    runs = list(para.findall(f"{{{WORD_NS}}}r"))
    if not runs:
        return False, rev_id

    # Remove runs from paragraph
    for run in runs:
        para.remove(run)

    # Wrap in deletion
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

    return True, rev_id


def _make_element(local_name, attribs):
    """Create a w: namespaced element with attributes."""
    elem = etree.Element(f"{{{WORD_NS}}}{local_name}")
    for k, v in attribs.items():
        elem.set(k, v)
    return elem


def _get_paragraph_text(para):
    """Get the full text content of a paragraph element."""
    texts = []
    for elem in para.iter():
        if elem.tag in (f"{{{WORD_NS}}}t", f"{{{WORD_NS}}}delText"):
            if elem.text:
                texts.append(elem.text)
    return "".join(texts)


def _enable_track_changes(temp_dir):
    """Enable track changes display in Word settings."""
    settings_path = os.path.join(temp_dir, "word", "settings.xml")
    if not os.path.exists(settings_path):
        return

    tree = etree.parse(settings_path)
    root = tree.getroot()

    # Remove existing trackChanges element if present
    for existing in root.findall(f"{{{WORD_NS}}}trackChanges"):
        root.remove(existing)

    # Add trackChanges element
    tc = etree.SubElement(root, f"{{{WORD_NS}}}trackChanges")
    tree.write(settings_path, xml_declaration=True, encoding="UTF-8", standalone=True)


def _repackage_docx(temp_dir, output_path):
    """Repackage extracted files back into a .docx zip."""
    with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for dirpath, dirnames, filenames in os.walk(temp_dir):
            for filename in filenames:
                file_path = os.path.join(dirpath, filename)
                arc_name = os.path.relpath(file_path, temp_dir)
                zf.write(file_path, arc_name)
