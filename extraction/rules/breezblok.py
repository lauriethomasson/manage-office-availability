"""Rule-based parser for Breezblok's single-listing marketing brochure PDF
(e.g. "John Stow House.pdf") — a multi-page document describing ONE
building, with a repeating footer block (Building name/Street/"London"/
Postcode) on most pages and one or more "Proposed space" sections giving
the actual floor/unit details.

Floor Plan/High Res Images aren't handled here — same as every other
LLM-fallback PDF source, those come from app.py's extraction.pdf_images
enrichment (position-based real image extraction), which runs for this
rule too (see app.py's _attach_pdf_images call-site)."""
import re

# The repeating footer block seen on most pages, e.g.:
#   John Stow House
#   18 Bevis Marks
#   London
#   EC3A 7JB
# Building name (any capitalized line), then a street line (starts with a
# house number), then the literal "London", then a full UK postcode.
_BUILDING_BLOCK_RE = re.compile(
    r"^([A-Z][A-Za-z0-9 '&\-]+?)\n"
    r"(\d[A-Za-z0-9 '&\-]+)\n"
    r"London\n"
    r"([A-Za-z]{1,2}\d[A-Za-z\d]?\s?\d[A-Za-z]{2})",
    re.MULTILINE,
)

_CONTACT_RE = re.compile(r"^Contact:?\s+(.+)$", re.IGNORECASE | re.MULTILINE)

# One listing's own details, e.g.:
#   Proposed space
#   Office 302- 1750 sqft
#   32 Desks and two meeting rooms
#   £18,000 per month + VAT
#   Deposit required to secure the office £36,000
_UNIT_SIZE_RE = re.compile(r"^(.+?)\s*-\s*([\d,]+)\s*sq\s*ft", re.IGNORECASE)
_DESKS_RE = re.compile(r"(\d+)\s*desks?", re.IGNORECASE)
_PRICE_RE = re.compile(r"£?\s*([\d,]+)\s*per\s*month", re.IGNORECASE)
_VAT_RE = re.compile(r"\+\s*VAT", re.IGNORECASE)
_DEPOSIT_RE = re.compile(r"deposit.*", re.IGNORECASE)


def detect(content):
    return "breezblok" in (content.get("text") or "").lower()


def parse(content):
    text = content.get("text") or ""
    building = _building(text)
    if not building:
        return []
    contact = _contact(text)

    lines = text.split("\n")
    records = []
    for i, line in enumerate(lines):
        if line.strip().lower() != "proposed space":
            continue
        block = lines[i + 1 : i + 5]
        record = _parse_unit_block(block)
        if not record:
            continue
        record["Building"] = building
        record["Contacts"] = contact
        records.append(record)
    return records


def _building(text):
    m = _BUILDING_BLOCK_RE.search(text)
    if not m:
        return ""
    name, street, postcode = (g.strip() for g in m.groups())
    return f"{name}, {street}, London {postcode}"


def _contact(text):
    m = _CONTACT_RE.search(text)
    return m.group(1).strip() if m else ""


def _parse_unit_block(lines):
    if not lines:
        return None
    unit_line = lines[0].strip() if len(lines) > 0 else ""
    desks_line = lines[1].strip() if len(lines) > 1 else ""
    price_line = lines[2].strip() if len(lines) > 2 else ""
    deposit_line = lines[3].strip() if len(lines) > 3 else ""

    size_match = _UNIT_SIZE_RE.match(unit_line)
    if not size_match:
        return None
    unit = size_match.group(1).strip()
    size = size_match.group(2).replace(",", "")

    desks_match = _DESKS_RE.search(desks_line)
    desks = desks_match.group(1) if desks_match else ""

    price_match = _PRICE_RE.search(price_line)
    price = price_match.group(1).replace(",", "") if price_match else ""

    features = [desks_line] if desks_line else []
    if _VAT_RE.search(price_line):
        features.append("Price excludes VAT")
    deposit_match = _DEPOSIT_RE.search(deposit_line)
    if deposit_match:
        features.append(deposit_match.group(0))

    return {
        "Floor/Unit": unit,
        "Size (sq ft)": size,
        "Desks (max)": desks,
        "Marketing Price (Based on Min Term) PCM": price,
        "Special Features": ". ".join(features),
    }
