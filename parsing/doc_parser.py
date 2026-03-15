"""Parse legacy .doc files using LibreOffice (Linux) or pywin32 COM (Windows)."""

import os
import platform
import shutil
import subprocess
import tempfile

IS_WINDOWS = platform.system() == "Windows"


def _ensure_extension(filepath, ext):
    """Copy file to a temp path with the correct extension if it lacks one.

    Both Word COM and LibreOffice require the file extension to identify format.
    Returns (path_to_use, needs_cleanup).
    """
    _, current_ext = os.path.splitext(filepath)
    if current_ext.lower() in (ext, ext.upper()):
        return filepath, False

    # Copy to temp file with correct extension
    temp_dir = tempfile.mkdtemp()
    basename = os.path.basename(filepath)
    temp_path = os.path.join(temp_dir, basename + ext)
    shutil.copy2(filepath, temp_path)
    return temp_path, True


# ---------------------------------------------------------------------------
# LibreOffice backend (Linux / cross-platform)
# ---------------------------------------------------------------------------

def _find_libreoffice():
    """Find the LibreOffice binary."""
    for name in ("libreoffice", "soffice"):
        path = shutil.which(name)
        if path:
            return path
    # Common install locations
    for path in ("/usr/bin/libreoffice", "/usr/bin/soffice",
                 "/snap/bin/libreoffice", "/usr/local/bin/libreoffice"):
        if os.path.isfile(path):
            return path
    return None


def _convert_with_libreoffice(filepath, output_format="docx"):
    """Convert a file using LibreOffice headless mode. Returns path to converted file."""
    lo_bin = _find_libreoffice()
    if not lo_bin:
        raise RuntimeError(
            "LibreOffice is not installed. Install it with: apt-get install libreoffice-writer"
        )

    filepath = os.path.abspath(filepath)
    work_path, needs_cleanup = _ensure_extension(filepath, ".doc")

    # Use a dedicated temp dir for output to avoid conflicts
    out_dir = tempfile.mkdtemp()
    try:
        result = subprocess.run(
            [lo_bin, "--headless", "--convert-to", output_format,
             "--outdir", out_dir, work_path],
            capture_output=True, text=True, timeout=120,
        )
        if result.returncode != 0:
            raise RuntimeError(f"LibreOffice conversion failed: {result.stderr}")

        # Find the converted file
        base = os.path.splitext(os.path.basename(work_path))[0]
        converted_path = os.path.join(out_dir, f"{base}.{output_format}")
        if not os.path.exists(converted_path):
            # Try to find any file with the right extension
            for f in os.listdir(out_dir):
                if f.endswith(f".{output_format}"):
                    converted_path = os.path.join(out_dir, f)
                    break
            else:
                raise RuntimeError(f"Converted file not found in {out_dir}")

        return converted_path, out_dir
    finally:
        if needs_cleanup:
            shutil.rmtree(os.path.dirname(work_path), ignore_errors=True)


def _parse_doc_libreoffice(filepath):
    """Parse .doc by converting to .docx with LibreOffice, then using python-docx."""
    from parsing.docx_parser import parse_docx

    converted_path, temp_dir = _convert_with_libreoffice(filepath, "docx")
    try:
        return parse_docx(converted_path)
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def _convert_doc_to_docx_libreoffice(filepath):
    """Convert .doc to .docx using LibreOffice. Returns path to new .docx."""
    converted_path, temp_dir = _convert_with_libreoffice(filepath, "docx")

    # Copy converted file to final destination next to original
    output_path = os.path.abspath(filepath) + ".docx"
    shutil.copy2(converted_path, output_path)
    shutil.rmtree(temp_dir, ignore_errors=True)
    return output_path


# ---------------------------------------------------------------------------
# pywin32 COM backend (Windows only)
# ---------------------------------------------------------------------------

def _parse_doc_win32(filepath):
    """Parse .doc using Word COM automation (Windows only)."""
    import pythoncom
    import win32com.client

    pythoncom.CoInitialize()
    filepath = os.path.abspath(filepath)
    work_path, needs_cleanup = _ensure_extension(filepath, ".doc")
    work_path = os.path.abspath(work_path)

    word = win32com.client.Dispatch("Word.Application")
    word.Visible = False
    try:
        doc = word.Documents.Open(work_path, ReadOnly=True)
        paragraphs = []
        for i in range(1, doc.Paragraphs.Count + 1):
            para = doc.Paragraphs(i)
            text = para.Range.Text.strip()
            if text:
                style_name = "Normal"
                try:
                    style_name = para.Style.NameLocal
                except Exception:
                    pass
                paragraphs.append({
                    "style": style_name,
                    "text": text,
                    "index": i - 1,
                })
        doc.Close(False)
        return paragraphs
    finally:
        word.Quit()
        pythoncom.CoUninitialize()
        if needs_cleanup:
            shutil.rmtree(os.path.dirname(work_path), ignore_errors=True)


def _convert_doc_to_docx_win32(filepath):
    """Convert .doc to .docx using Word COM automation (Windows only)."""
    import pythoncom
    import win32com.client

    pythoncom.CoInitialize()
    filepath = os.path.abspath(filepath)
    work_path, needs_cleanup = _ensure_extension(filepath, ".doc")
    work_path = os.path.abspath(work_path)
    output_path = filepath + ".docx"

    word = win32com.client.Dispatch("Word.Application")
    word.Visible = False
    try:
        doc = word.Documents.Open(work_path, ReadOnly=True)
        # wdFormatXMLDocument = 12
        doc.SaveAs2(os.path.abspath(output_path), FileFormat=12)
        doc.Close(False)
        return output_path
    finally:
        word.Quit()
        pythoncom.CoUninitialize()
        if needs_cleanup:
            shutil.rmtree(os.path.dirname(work_path), ignore_errors=True)


# ---------------------------------------------------------------------------
# Public API — auto-selects backend based on platform
# ---------------------------------------------------------------------------

def parse_doc(filepath):
    """Extract text from a .doc file.

    Uses pywin32 on Windows, LibreOffice on Linux/Mac.
    Returns a list of dicts with keys: style, text, index
    """
    if IS_WINDOWS:
        try:
            return _parse_doc_win32(filepath)
        except Exception:
            # Fall back to LibreOffice if available
            lo = _find_libreoffice()
            if lo:
                return _parse_doc_libreoffice(filepath)
            raise
    else:
        return _parse_doc_libreoffice(filepath)


def convert_doc_to_docx(filepath):
    """Convert a .doc file to .docx.

    Uses pywin32 on Windows, LibreOffice on Linux/Mac.
    Returns the path to the new .docx file.
    """
    if IS_WINDOWS:
        try:
            return _convert_doc_to_docx_win32(filepath)
        except Exception:
            lo = _find_libreoffice()
            if lo:
                return _convert_doc_to_docx_libreoffice(filepath)
            raise
    else:
        return _convert_doc_to_docx_libreoffice(filepath)


def get_full_text_doc(filepath):
    """Get full text from a .doc file."""
    paragraphs = parse_doc(filepath)
    return "\n".join(p["text"] for p in paragraphs)
