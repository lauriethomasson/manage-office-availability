"""Rule-based parser for MetSpace's "Weekly Availability" email layout.

Layout (plain text lines), fully repeated per listing (no carry-over state):
    AREA HEADER                (only before the first building in that area)
    Building name (1-2 lines, may wrap mid-word)
    [(parenthetical note)]     e.g. "(Monument)"
    [- Floor descriptor]
    Sqft: <n>
    Desks: <free text, e.g. "10 + MR + PB">
    Price: <n>
    Av: <date-ish>
"""
import re

from extraction.text_utils import titlecase_area

AREA_RE = re.compile(r"^[A-Z][A-Z ]+$")


def detect(content):
    blob = ((content.get("sender") or "") + " " + (content.get("text") or "")).lower()
    return "metspace" in blob


def parse(content):
    lines = [l.strip() for l in content["text"].split("\n") if l.strip()]
    contact = _contact_block(lines)

    records = []
    buffer = []
    current_area = ""
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        if line.lower().startswith("sqft:"):
            remaining = buffer
            buffer = []

            floor = ""
            if remaining and remaining[-1].startswith("-"):
                floor = remaining.pop().lstrip("- ").strip()
            parenthetical = ""
            if remaining and remaining[-1].startswith("(") and remaining[-1].endswith(")"):
                parenthetical = remaining.pop()

            # An area header may appear anywhere earlier in this block (or
            # not at all, if we're still under the same area as the last
            # listing) — everything before it is boilerplate/noise, so only
            # take what follows it as the actual building name. Scan
            # backwards so the *closest* area-like line wins (the true area
            # header sits right before the name; anything further back,
            # like email boilerplate, should be ignored).
            area_idx = next(
                (idx for idx in range(len(remaining) - 1, -1, -1) if AREA_RE.match(remaining[idx]) and len(remaining[idx].split()) <= 3),
                None,
            )
            if area_idx is not None:
                current_area = titlecase_area(remaining[area_idx])
                remaining = remaining[area_idx + 1 :]

            building = " ".join(remaining).strip()
            if parenthetical:
                building = f"{building} {parenthetical}".strip()

            if not building:
                i += 1
                continue

            sqft = line.split(":", 1)[1].strip() if ":" in line else ""
            desks_line = lines[i + 1] if i + 1 < n else ""
            price_line = lines[i + 2] if i + 2 < n else ""

            desks_desc = desks_line.split(":", 1)[1].strip() if desks_line.lower().startswith("desks:") else ""
            price_val = price_line.split(":", 1)[1].strip() if price_line.lower().startswith("price:") else ""

            records.append(
                {
                    "Area": current_area,
                    "Building": building,
                    "Floor/Unit": floor,
                    "Size (sq ft)": sqft.replace(",", ""),
                    "Desks (max)": _max_desks(desks_desc),
                    "Marketing Price (Based on Min Term) PCM": price_val.replace("£", "").replace(",", ""),
                    "Special Features": desks_desc,
                    "Contact 1": contact,
                }
            )
            i += 4  # consumed Sqft, Desks, Price, and Av lines
            continue
        buffer.append(line)
        i += 1

    return records


def _max_desks(desc):
    if not desc:
        return ""
    m = re.match(r"^\s*(\d+)\s*-\s*(\d+)", desc)
    if m:
        return m.group(2)
    m2 = re.search(r"\d+", desc)
    return m2.group() if m2 else ""


NAME_RE = re.compile(r"^[A-Z][a-zA-Z'.-]+(?: [A-Z][a-zA-Z'.-]+)+$")


def _contact_block(lines):
    """MetSpace emails end with a fixed, company-wide contact list — not
    per-listing info — shaped as repeating groups of
    Name / Title / Phone / Email / Website. Job titles can themselves look
    name-shaped (e.g. "Sales Manager" is two capitalized words), so rather
    than pattern-matching every line for "looks like a name", anchor on the
    one line in each group that's unambiguous — the website — and take the
    name as whatever sits exactly 4 lines before it."""
    try:
        idx = lines.index("Contact")
    except ValueError:
        return ""
    end = next((i for i in range(idx + 1, len(lines)) if lines[i].lower().startswith("copyright")), len(lines))
    block = lines[idx + 1 : end]

    names = []
    for i, line in enumerate(block):
        if line.lower().startswith("www.") and i >= 4:
            candidate = block[i - 4]
            if NAME_RE.match(candidate) and candidate not in names:
                names.append(candidate)
    return ", ".join(names)
