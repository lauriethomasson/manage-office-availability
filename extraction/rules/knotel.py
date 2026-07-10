"""Rule-based parser for Knotel's "Availability" email layout.

Layout (as plain text, one item per line):
    AREA HEADER (all caps, e.g. "CITY", "WEST END")
    Building name                      <- only present for the first floor
    Full address, London POSTCODE      <- of a given building
    Available[ from <date>]
    London
    Floor descriptor
    Full address, London POSTCODE      (repeated)
    [View property] [Download Floorplan] [View Brochure] [View Listing]  (buttons, order varies)
    Seats
    <n>
    Size
    <n> sqft
    Price (monthly)
    £<n> pcm
    Price (per sqft)
    £<n> per sqft

A building with multiple floors repeats from "Available" onward without
repeating the name/address lines.
"""
import re

from extraction.text_utils import titlecase_area

AREA_RE = re.compile(r"^[A-Z][A-Z ]+$")
POSTCODE_HINT_RE = re.compile(r"[A-Z]{1,2}\d[A-Z\d]?\s*\d[A-Z]{2}", re.IGNORECASE)
LINK_LABELS = ("View property", "Download Floorplan", "View Brochure", "View Listing", "View Floorplan")


def detect(content):
    blob = ((content.get("sender") or "") + " " + (content.get("text") or "")[:3000]).lower()
    if "knotel" in blob:
        return True
    return any("knotel.com" in href.lower() for _, href in content.get("links", []))


def parse(content):
    lines = [l.strip() for l in content["text"].split("\n") if l.strip()]
    link_groups = _group_items(content.get("html_items", []))

    records = []
    current_area = ""
    current_building = ""
    group_idx = 0
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]

        # Area header: short, all-caps, letters/spaces only, and not itself
        # an address/label line.
        if AREA_RE.match(line) and len(line.split()) <= 3 and not POSTCODE_HINT_RE.search(line):
            # Guard against matching things like "SEATS" etc. — area headers
            # are followed by a building name then an address line.
            if i + 2 < n and POSTCODE_HINT_RE.search(lines[i + 2]) or (i + 1 < n and POSTCODE_HINT_RE.search(lines[i + 1])):
                current_area = titlecase_area(line)
                i += 1
                continue

        if line == "Available" or line.startswith("Available from"):
            # Optionally preceded (immediately, before this Available block)
            # by a fresh "building name" + "address" pair.
            if i >= 2 and POSTCODE_HINT_RE.search(lines[i - 1]) and not lines[i - 2].startswith("Available"):
                current_building = lines[i - 2]

            floor = lines[i + 2] if i + 2 < n else ""

            # Scan forward (bounded) for the four labeled values.
            window = lines[i : i + 25]
            seats = _value_after(window, "Seats")
            size = _value_after(window, "Size")
            price_monthly = _value_after(window, "Price (monthly)")
            price_psf = _value_after(window, "Price (per sqft)")

            group = link_groups[group_idx] if group_idx < len(link_groups) else {}
            group_idx += 1

            records.append(
                {
                    "Area": current_area,
                    "Building": current_building,
                    "Floor/Unit": floor,
                    "Size (sq ft)": _strip_units(size, "sqft"),
                    "Desks (max)": seats,
                    "Marketing Price (Based on Min Term) PCM": _strip_units(price_monthly, "pcm"),
                    "Marketing Price (Based on Min Term) PSF": _strip_units(price_psf, "per sqft"),
                    "Link to File": group.get("brochure", ""),
                    "Floor Plan": group.get("floorplan", ""),
                    "High Res Images": group.get("highres", ""),
                }
            )
        i += 1

    return records


def _value_after(window, label):
    for idx, l in enumerate(window):
        if l == label and idx + 1 < len(window):
            return window[idx + 1]
    return ""


def _strip_units(value, *units):
    if not value:
        return ""
    v = value
    for u in units:
        v = re.sub(re.escape(u), "", v, flags=re.IGNORECASE)
    return v.replace("£", "").strip()


def _group_items(items):
    """Chunk the ordered (kind, text_or_alt, href_or_src) stream — from
    extraction.file_readers' html_items — into one dict per listing, keyed
    by which button labels (and photo, see below) were found. A new group
    starts at "View property" (the black primary button), which every
    listing has first.

    Confirmed empirically (2026-07) that each listing's own card is laid
    out in this literal DOM order: an <img alt="... featured image">
    (a real, listing-specific photo — not a logo/icon), then floor/address
    text, then this same button row. So the most recently seen "featured
    image" src, at the moment a new group starts, belongs to that group —
    reset immediately after so it can't leak into a later listing that
    has no photo of its own."""
    groups = []
    current = None
    pending_image = None
    for kind, a, b in items:
        if kind == "image":
            if "featured image" in a.lower():
                pending_image = b
            continue

        text, href = a, b
        if text not in LINK_LABELS:
            continue
        if text == "View property":
            current = {}
            groups.append(current)
            if pending_image:
                current["highres"] = pending_image
                pending_image = None
        if current is None:
            current = {}
            groups.append(current)
        low = text.lower()
        if "brochure" in low:
            current["brochure"] = href
        elif "floorplan" in low:
            current["floorplan"] = href
        elif "listing" in low:
            current["listing"] = href
        elif "property" in low:
            current["property"] = href
    return groups
