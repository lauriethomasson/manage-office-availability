"""Per-file-type raw content extraction.

Each reader returns a dict with at least:
  text  - plain readable text (for LLM fallback / line-based rules)
  html  - raw HTML string, when the source is HTML/eml (for link-aware rules)
  links - list of (visible_text, href) tuples in document order, when available
  tables - list of tables, each a list of rows, each row a list of cell strings

Readers raise ValueError with a human-readable message on failure — the
caller (pipeline) turns that into a per-file error in the results summary
instead of crashing the whole batch.
"""
import csv
import email
import re
from email import policy
from io import StringIO
from pathlib import Path

from bs4 import BeautifulSoup


def read_file(path):
    ext = Path(path).suffix.lower()
    try:
        if ext == ".pdf":
            return _read_pdf(path)
        if ext == ".docx":
            return _read_docx(path)
        if ext in (".xlsx", ".xls"):
            return _read_xlsx(path)
        if ext == ".csv":
            return _read_csv(path)
        if ext == ".eml":
            return _read_eml(path)
        if ext in (".html", ".htm"):
            return _read_html(path)
    except ValueError:
        raise
    except Exception as e:
        raise ValueError(f"Could not read {ext} file: {e}")
    raise ValueError(f"Unsupported file type: {ext}")


def _clean_pdf_cell(cell):
    """PDF table cells often wrap long text across multiple physical lines;
    join them back into prose, without adding a stray space after a
    hyphenated line break (e.g. "All-\nInclusive" -> "All-Inclusive")."""
    if not cell:
        return ""
    out = ""
    for line in cell.replace("\r", "").split("\n"):
        line = line.strip()
        if not line:
            continue
        # Only treat a trailing "-" as a hyphenated word-break (join with no
        # space) when it directly follows a letter, e.g. "All-"; a standalone
        # dash like "CAT A -" is punctuation and should keep its space.
        hyphenated = len(out) >= 2 and out.endswith("-") and out[-2].isalnum()
        out = out + line if hyphenated else (out + " " + line).strip()
    return out


def _empty(**overrides):
    base = {"text": "", "html": "", "links": [], "tables": []}
    base.update(overrides)
    return base


def _read_pdf(path):
    import pdfplumber

    text_parts = []
    tables = []
    try:
        with pdfplumber.open(path) as pdf:
            if not pdf.pages:
                raise ValueError("PDF has no pages")
            for page in pdf.pages:
                page_text = page.extract_text() or ""
                if page_text:
                    text_parts.append(page_text)
                for t in page.extract_tables() or []:
                    cleaned = [[_clean_pdf_cell(c) for c in row] for row in t]
                    if cleaned:
                        tables.append(cleaned)
    except Exception as e:
        raise ValueError(f"Failed to parse PDF: {e}")

    text = "\n".join(text_parts).strip()
    if not text and not tables:
        raise ValueError("No extractable text found in PDF (it may be a scanned image)")
    return _empty(text=text, tables=tables)


def _read_docx(path):
    import docx

    try:
        d = docx.Document(path)
    except Exception as e:
        raise ValueError(f"Failed to parse DOCX: {e}")

    text_parts = [p.text for p in d.paragraphs if p.text.strip()]
    tables = []
    for t in d.tables:
        rows = [[cell.text.strip() for cell in row.cells] for row in t.rows]
        if rows:
            tables.append(rows)
            for row in rows:
                text_parts.append(" | ".join(row))

    text = "\n".join(text_parts).strip()
    if not text:
        raise ValueError("No text found in DOCX")
    return _empty(text=text, tables=tables)


def _read_xlsx(path):
    import pandas as pd

    try:
        sheets = pd.read_excel(path, sheet_name=None, dtype=str)
    except Exception as e:
        raise ValueError(f"Failed to parse spreadsheet: {e}")

    tables = []
    text_parts = []
    for name, df in sheets.items():
        df = df.fillna("")
        header = [str(c) for c in df.columns]
        rows = [header] + df.astype(str).values.tolist()
        tables.append(rows)
        text_parts.append(f"[Sheet: {name}]")
        for row in rows:
            text_parts.append(" | ".join(row))

    if not tables or all(len(t) <= 1 for t in tables):
        raise ValueError("Spreadsheet appears to have no data rows")
    return _empty(text="\n".join(text_parts), tables=tables)


def _read_csv(path):
    try:
        with open(path, newline="", encoding="utf-8-sig") as f:
            rows = list(csv.reader(f))
    except UnicodeDecodeError:
        with open(path, newline="", encoding="latin-1") as f:
            rows = list(csv.reader(f))
    except Exception as e:
        raise ValueError(f"Failed to parse CSV: {e}")

    rows = [r for r in rows if any(c.strip() for c in r)]
    if not rows:
        raise ValueError("CSV file is empty")

    buf = StringIO()
    for row in rows:
        buf.write(" | ".join(row) + "\n")
    return _empty(text=buf.getvalue(), tables=[rows])


def _read_eml(path):
    try:
        with open(path, "rb") as f:
            msg = email.message_from_binary_file(f, policy=policy.default)
    except Exception as e:
        raise ValueError(f"Failed to parse EML: {e}")

    html_body = None
    text_body = None
    for part in msg.walk():
        ctype = part.get_content_type()
        try:
            content = part.get_content()
        except Exception:
            continue
        if ctype == "text/html" and html_body is None:
            html_body = content
        elif ctype == "text/plain" and text_body is None:
            text_body = content

    if html_body:
        result = _parse_html_string(html_body)
    elif text_body:
        result = _empty(text=text_body.strip())
    else:
        raise ValueError("EML has no readable text or HTML body")

    result["subject"] = msg.get("Subject", "")
    result["sender"] = msg.get("From", "")
    if not result["text"].strip():
        raise ValueError("EML body is empty after parsing")
    return result


def _read_html(path):
    try:
        html = Path(path).read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        raise ValueError(f"Failed to read HTML file: {e}")
    result = _parse_html_string(html)
    if not result["text"].strip():
        raise ValueError("HTML file has no visible text")
    return result


def _parse_html_string(html):
    soup = BeautifulSoup(html, "lxml")
    # Some senders hard-wrap text with literal newlines *inside* a single
    # text node (e.g. "View\n property", "Shared\n garden..."), which is a
    # source-formatting artifact, not a real line break. Collapse internal
    # whitespace per node so each node becomes exactly one clean line, then
    # join nodes with "\n" — this keeps genuine element-to-element breaks.
    lines = [re.sub(r"\s+", " ", s).strip() for s in soup.stripped_strings]
    text = "\n".join(l for l in lines if l)
    links = [
        (re.sub(r"\s+", " ", a.get_text(" ", strip=True)).strip(), a.get("href", ""))
        for a in soup.find_all("a")
        if a.get("href")
    ]
    tables = []
    for t in soup.find_all("table"):
        rows = []
        for tr in t.find_all("tr"):
            cells = [c.get_text(" ", strip=True) for c in tr.find_all(["td", "th"])]
            if any(c for c in cells):
                rows.append(cells)
        if rows:
            tables.append(rows)
    return _empty(text=text, html=html, links=links, tables=tables)
