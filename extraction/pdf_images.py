"""Extracts real, embedded per-listing images from a PDF for sources with
no rule-based parser (LLM fallback) — e.g. BC, Crown Estate — as opposed
to Kitt's, which already gets Floor Plan/High Res Images from its own
table columns via extraction.rules.grid, or Knotel, which gets a Floor
Plan link from its email's own "Download Floorplan" button (extraction.
rules.knotel) — neither of those needs this module.

Confirmed empirically (2026-07) on the Crown Estate example that a
source's decorative/repeated assets (a "MANAGED - ALL INCLUSIVE RATES"
banner, a small footer logo) are byte-identical across many pages, while
a genuine per-listing photo is page-unique — so boilerplate is detected
by content hash, not by size/position, which varies too much to be a
reliable signal on its own.
"""
import hashlib
import re
from collections import defaultdict

_LEADING_NAME_RE = re.compile(r"^([A-Za-z][A-Za-z\s'-]*)")

# A real per-listing photo should be page-unique (or shared by only the
# handful of floors on the same building's page). Anything recurring on
# more pages than that is decorative branding repeated throughout the
# document, not a listing's own image.
BOILERPLATE_MAX_PAGES = 2


def extract_page_images(path):
    """Returns {page_num (0-indexed): [(image_bytes, ext), ...]} — real,
    non-boilerplate images only, deduped within a page, in first-seen
    order. Returns {} if PyMuPDF isn't installed or the PDF can't be
    opened/has no images — never raises, this is always an optional
    enrichment, not something that should fail extraction."""
    try:
        import fitz
    except ImportError:
        return {}

    try:
        doc = fitz.open(path)
    except Exception:
        return {}

    hash_pages = defaultdict(set)
    hash_bytes = {}
    hash_ext = {}
    page_hashes = defaultdict(list)

    try:
        for page_num in range(len(doc)):
            for img in doc[page_num].get_images(full=True):
                try:
                    base = doc.extract_image(img[0])
                except Exception:
                    continue
                data = base.get("image")
                if not data:
                    continue
                h = hashlib.sha256(data).hexdigest()
                hash_pages[h].add(page_num)
                hash_bytes[h] = data
                hash_ext[h] = base.get("ext", "png")
                page_hashes[page_num].append(h)
    finally:
        doc.close()

    boilerplate = {h for h, pages in hash_pages.items() if len(pages) > BOILERPLATE_MAX_PAGES}

    result = {}
    for page_num, hashes in page_hashes.items():
        real = list(dict.fromkeys(h for h in hashes if h not in boilerplate))
        if real:
            result[page_num] = [(hash_bytes[h], hash_ext[h]) for h in real]
    return result


def find_matching_page(building_name, pages_text):
    """Best-effort: the first page (0-indexed) whose extracted text
    contains `building_name` (case-insensitive), or None. pages_text is
    the list of per-page text strings from extraction.file_readers'
    _read_pdf. Relies on these documents' own layout convention — each
    building's name appears as a heading on its own page — confirmed
    empirically on the Crown Estate example; not a guarantee for every
    possible PDF layout.

    Tries the building name as given first, then two narrower fallbacks —
    since an LLM-extracted "Building" often joins a name and its street
    address (e.g. "Linen Hall, 162-168 Regent Street" or "Linen Hall
    162-168 Regent Street") while the source PDF's own text has them as
    separate lines/tokens, sometimes with the name repeated first (a
    two-column page layout printing "LINEN HALL LINEN HALL" before the
    address starts) — which breaks a match on the full joined string
    even after normalizing punctuation. Confirmed empirically on Linen
    Hall/Princes House/Crown House/Maddox House/Kendal House: only the
    bare name portion (stopping at the first digit or comma) reliably
    matches in these cases.
      1. the building name as given
      2. the part before the first comma, if any
      3. the leading run of letters/spaces (stops at the first digit,
         comma, parenthesis, or other punctuation — e.g. "Princes House"
         from both "Princes House 38 Jermyn Street" and "Princes House
         (38 Jermyn Street)", since the LLM's exact punctuation isn't
         deterministic call to call)
    A building name that's mostly numeric (e.g. "1 Vine Street") has no
    useful (3), so it relies on (1)/(2) instead — already confirmed
    working for those via the full-string match."""
    candidates = [building_name or ""]
    if "," in (building_name or ""):
        candidates.append(building_name.split(",", 1)[0])
    m = _LEADING_NAME_RE.match(building_name or "")
    if m and m.group(1).strip():
        candidates.append(m.group(1))

    for candidate in candidates:
        name = candidate.strip().lower()
        if not name or not pages_text:
            continue
        for i, text in enumerate(pages_text):
            if name in (text or "").lower():
                return i
    return None
