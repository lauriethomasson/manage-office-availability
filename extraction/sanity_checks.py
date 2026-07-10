"""Generic post-extraction sanity check: flags a field that came back
blank for every single row in one processed file. A per-row gap is
normal — a real listing commonly lacks one data point — but it's rare
for a whole source document to genuinely have NONE of a field the
extraction layer is supposed to populate (e.g. Contacts, Floor Plan,
Special Features). Two real bugs already slipped through this way
(GPE's Contacts, Knotel's High Res Images) before either was noticed by
manual spot-checking; this catches the next one automatically instead.

Deliberately generic, not tied to any specific field or source - the
point is to catch whichever field breaks next, not just the ones
already fixed.
"""
from .schema import SOURCE_FIELDS

# Fields the extraction layer (a rule module or the LLM fallback) is
# actually responsible for populating - checked here. Kato/derived fields
# (Property Postcode, Lat, Lng, Assigned Agents, For Sale, To Let,
# External Ref) already have their own established blank/placeholder
# conventions (e.g. "Needs manual lookup") handled elsewhere and aren't
# covered by this generic check.
_CHECKED_FIELDS = SOURCE_FIELDS


def find_blank_field_warnings(records, label):
    """Returns a list of human-readable warning strings, one per field
    that's blank for every record in `records`. Never raises; returns []
    for an empty list (nothing to check). `label` names the source in
    the message (e.g. a provider name) — purely cosmetic.

    This will sometimes flag a field that's genuinely, correctly blank
    for an entire source (e.g. a PDF with no embedded images at all has
    nothing to put in High Res Images) — that's an expected, acceptable
    false positive here, not a bug in the check: the point is a fast,
    reviewable signal during testing, not a hard failure a human doesn't
    need to look at."""
    if not records:
        return []
    warnings = []
    for field in _CHECKED_FIELDS:
        if all(_is_blank(r.get(field)) for r in records):
            warnings.append(f"{field} is blank for all {len(records)} {label} rows — check extraction logic")
    return warnings


def _is_blank(v):
    return v is None or v == ""
