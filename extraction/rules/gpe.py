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
NAME_RE = re.compile(r"^[A-Z][a-zA-Z'.-]+(?: [A-Z][a-zA-Z'.-]+)+$")


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
    contact = _contact_block(lines)
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
                            "Contacts": contact,
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

    photo_by_building = _photo_by_building(content.get("html_items", []))
    for record in records:
        photo = photo_by_building.get(record.get("Building"))
        if photo:
            record["High Res Images"] = photo

    return records


def _photo_by_building(html_items):
    """Maps each building name to its real marketing photo — a genuine
    hosted assets-gbr.mkt.dynamics.com image (GPE's own images have no
    distinguishing alt text, same situation as MetSpace, so this filters
    by source domain instead), keyed by building rather than by
    occurrence: confirmed empirically GPE's email links each building's
    name exactly once (in the "CURRENT AVAILABILITY" section), not once
    per floor/unit row the way MetSpace's does — so every floor of the
    same building shares its one photo, applied by name lookup below
    rather than by sequential position.

    Confirmed by re-reading the raw HTML after an initial implementation
    got this backwards: each building's photo comes immediately AFTER
    that building's own link, not before it (a strict, alternating
    LINK-then-IMAGE-then-LINK-then-IMAGE... run through all 9 buildings
    in the "CURRENT AVAILABILITY" section, one real photo each, none
    missing). The first version of this function tracked "the last image
    seen, attribute it to the next link" — which silently attributed
    every building's real photo to the *following* building instead, and
    dropped the very first ("16 Dufour's Place", nothing precedes it)
    and the very last building's photo entirely (followed by "Enquire
    now", not another building link). Confirmed by actually viewing
    several: every one is a real building photo, not a floor plan
    diagram — no separate floor-plan-labeled image exists anywhere in
    this source, so Floor Plan is deliberately left untouched here.

    A stray image can follow an unrelated link too (a promo "Find out
    more"/"Visit website" button, or an empty-text footer/social icon) —
    harmless here since no record's Building will ever equal that link's
    text, so the lookup below simply never matches it. Only the first
    image right after a link counts — further images before the next
    link (e.g. several footer/social icons in a row) aren't attributed
    to anything, since confirmed empirically each real listing has
    exactly one photo, not several."""
    photo_by_building = {}
    pending_building = None
    for kind, a, b in html_items:
        if kind == "link":
            pending_building = a
            continue
        # image
        if pending_building and "digitalassets/images" in b:
            if pending_building not in photo_by_building:
                photo_by_building[pending_building] = b
            pending_building = None
    return photo_by_building


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


def _contact_block(lines):
    """GPE emails end with a fixed, whole-email "Get in touch" block, not
    per-listing info — same idea as MetSpace's own contact block. Shaped
    as repeating Name / phone-number-line(s) groups (a phone number is
    sometimes hard-wrapped across two lines, e.g. "+44 (0) 7435 9" then
    "39 956" — a source formatting quirk), terminated by "View in
    browser". Rather than parse the phone number's own shape (fragile
    given that wrapping), just keep whichever lines look like a person's
    name and skip everything else in the block."""
    try:
        idx = lines.index("Get in touch")
    except ValueError:
        return ""
    end = next((i for i in range(idx + 1, len(lines)) if lines[i] == "View in browser"), len(lines))
    names = [l for l in lines[idx + 1 : end] if NAME_RE.match(l)]
    seen = []
    for n in names:
        if n not in seen:
            seen.append(n)
    return ", ".join(seen)
