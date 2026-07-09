"""Generates a readable PDF snapshot of a non-PDF source file, so
Link to Brochure can always point at a PDF regardless of the original
file type (.eml, .docx, .xlsx, .xls, .csv, .html/.htm).

This is not a pixel-perfect conversion of the original document — Render's
plain Python buildpack has no LibreOffice/Word/wkhtmltopdf available to do
that faithfully (docx2pdf, for example, shells out to Word on Windows or
LibreOffice elsewhere, neither of which exists there). Instead this
renders the same readable text every non-PDF file_readers.py reader
already extracts into `content["text"]` — which already includes table
rows inlined as plain text for DOCX/XLSX/CSV, and full reading-order text
for HTML/eml — using a pure-Python PDF writer (fpdf2) with no system
dependencies at all.
"""
from fpdf import FPDF

MAX_BODY_CHARS = 60000  # generous, but bounds a pathological huge input

# fpdf2's core fonts only support latin-1. Written as \uXXXX escapes
# (rather than literal characters) so this survives any tool/editor
# round-trip intact. U+2011 (non-breaking hyphen) is a real one, found in
# actual source content — an email signature's "Silver House, 31‑35
# Beak Street" renders as "31?35" without this mapping.
_REPLACEMENTS = {
    "—": "-",  # em dash
    "–": "-",  # en dash
    "‐": "-",  # hyphen
    "‑": "-",  # non-breaking hyphen
    "‒": "-",  # figure dash
    "―": "-",  # horizontal bar
    "­": "",   # soft hyphen
    "‘": "'", "’": "'",  # curly single quotes
    "“": '"', "”": '"',  # curly double quotes
    "…": "...",  # ellipsis
    " ": " ",  # non-breaking space
}


def _clean(text):
    """Swap common "smart" Unicode punctuation for its ASCII equivalent
    first, then replace anything else latin-1 can't encode rather than
    crashing on it."""
    if not text:
        return ""
    for src, dst in _REPLACEMENTS.items():
        text = text.replace(src, dst)
    return text.encode("latin-1", errors="replace").decode("latin-1")


def _write_cell(pdf, width, height, text):
    """multi_cell, but always from an explicit left-margin x and an
    explicit width — never relying on w=0's "rest of the line" inference
    or on a previous call having reset x/y the way we expect. Skips
    silently (rather than raising) on any fpdf2 rendering error, since a
    snapshot PDF is a best-effort artifact, not a critical-path one — one
    malformed line shouldn't lose the rest of the document."""
    text = _clean(text)
    if not text:
        return
    pdf.set_x(pdf.l_margin)
    try:
        pdf.multi_cell(width, height, text)
    except Exception as e:
        print(f"[pdf_snapshot] skipped a line that fpdf2 couldn't render: {e}")


def build_snapshot_pdf(path, title, subtitle_lines=None, body_text=None):
    pdf = FPDF()
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.add_page()
    width = pdf.epw  # effective page width (full width minus margins)

    pdf.set_font("Helvetica", "B", 16)
    _write_cell(pdf, width, 10, title or "Untitled")

    if subtitle_lines:
        pdf.set_font("Helvetica", "", 10)
        for line in subtitle_lines:
            _write_cell(pdf, width, 6, line)

    body_text = (body_text or "").strip()
    if body_text:
        if len(body_text) > MAX_BODY_CHARS:
            body_text = body_text[:MAX_BODY_CHARS] + "\n\n[... truncated ...]"
        pdf.set_font("Helvetica", "", 11)
        # One multi_cell per line rather than one call for the whole body —
        # keeps a single malformed line from losing everything after it,
        # and sidesteps whatever in fpdf2's line-break algorithm produced
        # "not enough horizontal space" on an otherwise ordinary short
        # line when it followed straight after another multi_cell call.
        for line in body_text.splitlines():
            _write_cell(pdf, width, 6, line if line.strip() else " ")

    pdf.output(str(path))


def snapshot_for_content(path, content, fallback_title):
    """Builds a snapshot PDF at `path` from a file_readers.read_file()
    content dict. `fallback_title` is used when the content has no
    subject of its own (anything but .eml)."""
    title = (content or {}).get("subject") or fallback_title
    subtitle_lines = []
    sender = (content or {}).get("sender")
    date = (content or {}).get("date")
    if sender:
        subtitle_lines.append(f"From: {sender}")
    if date:
        subtitle_lines.append(f"Date: {date}")

    build_snapshot_pdf(
        path,
        title=title,
        subtitle_lines=subtitle_lines,
        body_text=(content or {}).get("text"),
    )
