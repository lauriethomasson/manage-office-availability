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

# A tiny logo/icon fragment can be page-unique too (confirmed empirically
# on a BC brochure: a single 675-byte partial-logo image on one page,
# evading the boilerplate-by-repetition check above) — every genuine
# photo seen so far is tens of KB at minimum, so this threshold has a
# wide, safe margin rather than sitting right at the boundary.
MIN_IMAGE_BYTES = 3000


def scan_pages(path):
    """Pass 1 of the memory-bounded extraction split (see load_page_images
    for pass 2): identifies which pages have real, non-boilerplate images
    and which content-hashes those are, WITHOUT retaining any image's raw
    bytes past the moment it's hashed.

    Confirmed empirically (2026-07) on a large Render free-tier PDF (Crown
    Estate, 4.3MB) that the original single-pass version of this module —
    decode every image on every page, then keep every one of them (deduped
    by hash, but still the full set of distinct real images in the whole
    document) alive in memory for as long as the caller took to walk every
    record and upload each match — could hold much more of a document's
    embedded photography in memory at once than any single page ever
    needs, on a document with many/large real photos (a 20-30MB brochure
    with high-resolution per-listing photography is a real, plausible
    document even though this specific example's images totaled a modest
    few MB). Splitting into a cheap hash-only scan (this function) plus an
    on-demand, one-page-at-a-time loader (load_page_images) bounds peak
    memory to roughly one page's own images, regardless of how large or
    image-heavy the document as a whole is.

    Returns {page_num (0-indexed): [hash, ...]} for pages with at least
    one real (non-boilerplate, non-tiny) image, in first-seen order — {}
    if PyMuPDF isn't installed or the PDF can't be opened/has no images."""
    try:
        import fitz
    except ImportError:
        return {}

    try:
        doc = fitz.open(path)
    except Exception:
        return {}

    hash_pages = defaultdict(set)
    page_hashes = defaultdict(list)

    try:
        for page_num in range(len(doc)):
            for img in doc[page_num].get_images(full=True):
                try:
                    base = doc.extract_image(img[0])
                except Exception:
                    continue
                data = base.get("image")
                if not data or len(data) < MIN_IMAGE_BYTES:
                    continue
                h = hashlib.sha256(data).hexdigest()
                hash_pages[h].add(page_num)
                page_hashes[page_num].append(h)
                # `data`/`base` go out of scope at the end of this
                # iteration — deliberately never stashed anywhere past
                # hashing; that's the whole point of this pass.
    finally:
        doc.close()

    boilerplate = {h for h, pages in hash_pages.items() if len(pages) > BOILERPLATE_MAX_PAGES}

    result = {}
    for page_num, hashes in page_hashes.items():
        real = list(dict.fromkeys(h for h in hashes if h not in boilerplate))
        if real:
            result[page_num] = real
    return result


def load_page_images(path, page_num, allowed_hashes):
    """Pass 2: re-opens the PDF just to decode the real images on ONE
    specific page, filtered to `allowed_hashes` (that page's own entry
    from scan_pages' result, above) so a page that also contains a
    boilerplate banner doesn't return it a second way. Called once per
    (page, record-match) from the caller's own loop, immediately after
    which the caller uploads and discards the bytes — so at most one
    page's real images are ever resident at once, never the whole
    document's.

    Deliberately reopens the file per call rather than keeping one
    fitz.Document handle open across many calls: PyMuPDF's own per-page
    decode cost is small (confirmed empirically: ~0.1s for this module's
    entire single-pass extraction across a whole 20-page brochure), so
    trading a little redundant CPU for never holding more than one page's
    images at a time is the right side of that tradeoff here. Returns
    [(image_bytes, ext), ...] in first-seen order — [] if PyMuPDF isn't
    installed, the PDF can't be reopened, or the page index is out of
    range; never raises."""
    try:
        import fitz
    except ImportError:
        return []

    try:
        doc = fitz.open(path)
    except Exception:
        return []

    try:
        if page_num < 0 or page_num >= len(doc):
            return []
        allowed = set(allowed_hashes)
        seen = set()
        result = []
        for img in doc[page_num].get_images(full=True):
            try:
                base = doc.extract_image(img[0])
            except Exception:
                continue
            data = base.get("image")
            if not data or len(data) < MIN_IMAGE_BYTES:
                continue
            h = hashlib.sha256(data).hexdigest()
            if h not in allowed or h in seen:
                continue
            seen.add(h)
            result.append((data, base.get("ext", "png")))
        return result
    finally:
        doc.close()


def extract_page_images(path):
    """Returns {page_num (0-indexed): [(image_bytes, ext), ...]} — real,
    non-boilerplate, non-tiny images only, deduped within a page, in
    first-seen order. Returns {} if PyMuPDF isn't installed or the PDF
    can't be opened/has no images — never raises, this is always an
    optional enrichment, not something that should fail extraction.

    A convenience wrapper around scan_pages + load_page_images that
    materializes every page's images at once, same as this function's
    original (single-pass) implementation — kept for callers that
    genuinely want the whole document's images together (e.g. this
    module's own test coverage) and don't need the peak-memory bound
    app.py's real request path cares about; see _attach_pdf_images in
    app.py for the page-by-page caller that actually needs that bound."""
    page_hashes = scan_pages(path)
    return {page_num: load_page_images(path, page_num, hashes) for page_num, hashes in page_hashes.items()}


_FLOORPLAN_TEXT_RE = re.compile(r"floor[\s-]?plan", re.IGNORECASE)


def find_matching_pages(building_name, pages_text):
    """Best-effort: every page (0-indexed) whose extracted text contains
    `building_name` (case-insensitive), using whichever candidate tier
    below is the first to match at least one page — never a mix of
    tiers. pages_text is the list of per-page text strings from
    extraction.file_readers' _read_pdf. Relies on these documents' own
    layout convention — a listing's own building name appears as a
    heading on every page describing it — confirmed empirically on the
    Crown Estate example (one building per page or page-pair) and on a
    single-listing brochure (BC's own "2-7 Clerkenwell Green" brochure,
    where the name recurs across all 10 pages); not a guarantee for
    every possible PDF layout.

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
        matches = [i for i, text in enumerate(pages_text) if name in (text or "").lower()]
        if matches:
            return matches
    return []


def is_floorplan_page(page_text):
    """True if a page's own text calls out its content as a floor plan
    (e.g. BC's brochure literally has a page headed "Example Floorplan").
    A real, source-labeled signal, not a guess from pixel content — but
    confirmed empirically (Breezblok's John Stow House brochure) that not
    every source labels its floor-plan page this way, so this alone isn't
    enough; see is_floorplan_image for a per-image fallback."""
    return bool(_FLOORPLAN_TEXT_RE.search(page_text or ""))


# A CAD-rendered floor-plan diagram is drawn on a plain white/pale
# background, unlike either a real photo or a decorative logo/icon
# graphic — confirmed empirically across BC's and Breezblok's own real
# floor-plan images (69.7% and 84.9% of pixels near-white respectively)
# versus every real photo tested (never above ~17%) and Breezblok's own
# decorative header/footer illustration (34.5% — low unique-color count
# like a floor plan, but nowhere near as white). 0.5 sits with a wide
# margin on both sides of that gap.
FLOORPLAN_WHITE_FRACTION = 0.5
# Only need a rough estimate, so large images are downsampled first —
# this keeps the check cheap even for a multi-megapixel source image.
_MAX_SAMPLE_PIXELS = 400 * 400
_SAMPLE_WIDTH = 200


def _white_fraction(image_bytes):
    """Fraction of an image's pixels that are near-white (all three RGB
    channels above 235). Returns 0.0 (never mistaken for a floor plan) if
    Pillow can't decode it, rather than raising — this is only ever an
    extra signal on top of the existing hash/size-based filtering, never
    something that should fail extraction."""
    try:
        from PIL import Image
    except ImportError:
        return 0.0
    try:
        import io

        im = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    except Exception:
        return 0.0

    w, h = im.size
    if w * h > _MAX_SAMPLE_PIXELS and w > 0:
        im = im.resize((_SAMPLE_WIDTH, max(1, int(_SAMPLE_WIDTH * h / w))))

    total = 0
    white = 0
    for r, g, b in im.getdata():
        total += 1
        if r > 235 and g > 235 and b > 235:
            white += 1
    return (white / total) if total else 0.0


def is_floorplan_image(image_bytes):
    """True if an image's own pixel content looks like a floor-plan
    diagram rather than a photo or decorative graphic (see
    FLOORPLAN_WHITE_FRACTION). Deliberately a per-image check, not a
    per-page one: confirmed on Breezblok's John Stow House brochure that
    a floor-plan diagram and a real desk photo can share the same PDF
    page, so classifying by page alone would wrongly keep or exclude
    both together."""
    return _white_fraction(image_bytes) > FLOORPLAN_WHITE_FRACTION


def build_gallery_html(title, image_urls):
    """A minimal, self-contained HTML page listing several image URLs
    stacked together — used when a listing has more than one real photo,
    since a spreadsheet cell can only hold one hyperlink. No JS, no
    external assets — just <img> tags pointing at each image's own
    already-hosted download URL."""
    import html as _html

    safe_title = _html.escape(title or "Photos")
    imgs_html = "\n".join(
        f'<img src="{_html.escape(url)}" alt="Photo {i + 1}" '
        f'style="max-width:100%; height:auto; display:block; margin-bottom:16px; border-radius:6px;">'
        for i, url in enumerate(image_urls)
    )
    return (
        "<!doctype html>\n<html>\n<head>\n<meta charset=\"utf-8\">\n"
        f"<title>{safe_title} — Photos</title>\n"
        "<style>\n"
        "  body { font-family: -apple-system, Segoe UI, Helvetica, Arial, sans-serif; "
        "background:#0f1115; color:#e6e6e6; margin:0; padding:24px; }\n"
        "  h1 { font-size:18px; margin:0 0 16px; }\n"
        "  .gallery { max-width:900px; margin:0 auto; }\n"
        "</style>\n</head>\n<body>\n<div class=\"gallery\">\n"
        f"<h1>{safe_title}</h1>\n{imgs_html}\n</div>\n</body>\n</html>"
    )
