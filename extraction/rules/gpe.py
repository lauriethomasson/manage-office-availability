"""Rule-based parser for GPE's "Fully Managed Availability" email layout.

Layout (plain text lines):
    AREA HEADER (all caps, e.g. "SOHO", "ST JAMES'S")
    Building name
    Description sentence(s)
    - bullet feature
    - bullet feature ...
    Available
    Desk space
    Sq ft
    Price (psf)
    [optional promo note line]
    <floor label>          \
    <desk range> desks      | repeated once per floor/unit in the building
    <sqft>                  |
    <£psf>                 /
    ... (next building, or next area, starts here)

A building can have several floor rows in a row; PCM isn't given directly
here, only PSF — schema.normalize_record derives PCM from PSF * size / 12.
"""
import re

from extraction.text_utils import titlecase_area

AREA_RE = re.compile(r"^[A-Z][A-Z '\"]+$")
DESKS_LINE_RE = re.compile(r"\bdesks?\b", re.IGNORECASE)
SQFT_LINE_RE = re.compile(r"^[\d,]+(\s*-\s*[\d,]+)?$")
PSF_LINE_RE = re.compile(r"^£[\d,.]+(\s*-\s*£?[\d,.]+)?$")
MARKER = ("Available", "Desk space", "Sq ft", "Price (psf)")


def detect(content):
    blob = ((content.get("sender") or "") + " " + (content.get("text") or "")).lower()
    if "gpe.co.uk" in blob or "fully managed" in blob:
        return True
    return False


def parse(content):
    lines = _clean_lines(content["text"])
    # Everything before "CURRENT AVAILABILITY" is marketing/news content
    # (with its own ALL-CAPS-looking headings that would otherwise be
    # mistaken for area names) — drop it.
    try:
        lines = lines[lines.index("CURRENT AVAILABILITY") + 1 :]
    except ValueError:
        pass
    records = []
    current_area = ""
    buffer = []
    i = 0
    n = len(lines)
    while i < n:
        if lines[i : i + 4] == list(MARKER):
            header = buffer
            buffer = []
            # An area header, when present, sits right before the building
            # name — scan backwards so it wins over any earlier noise (CTA
            # button labels, or for the very first block, marketing copy).
            area_idx = next(
                (idx for idx in range(len(header) - 1, -1, -1) if AREA_RE.match(header[idx]) and len(header[idx].split()) <= 4),
                None,
            )
            if area_idx is not None:
                current_area = titlecase_area(header[area_idx])
                header = header[area_idx + 1 :]
            building = header[0] if header else ""
            features = "; ".join(l.lstrip("- ").strip() for l in header[1:] if l.startswith("-"))

            j = i + 4
            skipped_notes = 0
            found_any = False
            while j + 3 < n and skipped_notes <= 2:
                floor, desks_line, sqft_line, psf_line = lines[j : j + 4]
                if DESKS_LINE_RE.search(desks_line) and SQFT_LINE_RE.match(sqft_line) and PSF_LINE_RE.match(psf_line):
                    records.append(
                        {
                            "Area": current_area,
                            "Building": building,
                            "Floor/Unit": floor,
                            "Size (sq ft)": sqft_line.split("-")[-1].strip().replace(",", ""),
                            "Desks (max)": _max_desks(desks_line),
                            "Marketing Price (Based on Min Term) PSF": psf_line.split("-")[-1].replace("£", "").strip(),
                            "Special Features": features,
                        }
                    )
                    found_any = True
                    j += 4
                    continue
                if not found_any and skipped_notes < 2:
                    skipped_notes += 1
                    j += 1
                    continue
                break
            i = j
            continue
        buffer.append(lines[i])
        i += 1

    return records


def _clean_lines(text):
    raw = [l.strip() for l in text.split("\n") if l.strip()]
    # A lone "-" bullet marker occasionally lands on its own line, separate
    # from its text (a source formatting quirk) — merge it with the next line.
    cleaned = []
    i = 0
    while i < len(raw):
        if raw[i] == "-" and i + 1 < len(raw):
            cleaned.append("- " + raw[i + 1])
            i += 2
        else:
            cleaned.append(raw[i])
            i += 1
    return cleaned


def _max_desks(desc):
    m = re.match(r"^\s*(\d+)\s*-\s*(\d+)", desc)
    if m:
        return m.group(2)
    m2 = re.search(r"\d+", desc)
    return m2.group() if m2 else ""
