"""Resolves an output spreadsheet name for each processed file, and keeps
names unique within a batch.
"""
import re
from email.utils import parsedate_to_datetime
from pathlib import Path

# Rules tied to a specific, known sender — the rule name itself IS the
# provider name. The generic "Grid/Tabular" rule matches any tabular input
# and isn't tied to a sender, so it doesn't count as a confident identification.
NAMED_RULES = {"Knotel", "MetSpace", "GPE", "BC", "Breezblok"}

ILLEGAL_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*]')
LEADING_REPLY_PREFIX = re.compile(r"^(fw|fwd|re)[:_\-\s]+", re.IGNORECASE)


def resolve_provider_name(rule_name, filename, llm_source_name=None):
    """rule_name: the name returned by extraction.rules.try_rules(), or None
    if nothing matched (LLM fallback was used). llm_source_name: the source
    name the LLM identified, if the LLM fallback was used."""
    if rule_name in NAMED_RULES:
        return rule_name
    if llm_source_name:
        cleaned = _sanitize(llm_source_name)
        if cleaned:
            return cleaned
    return _name_from_filename(filename)


def extract_date(content):
    """Best-effort YYYY-MM-DD from an email's Date header, or None."""
    date_header = (content or {}).get("date")
    if not date_header:
        return None
    try:
        return parsedate_to_datetime(date_header).strftime("%Y-%m-%d")
    except (TypeError, ValueError):
        return None


def resolve_source_date(content):
    """Best-effort YYYY-MM-DD for when the source document was actually
    created/sent, in priority order:
      1. An email's Date header (the actual sent date) — extract_date().
      2. PDF/DOCX metadata (creation date, then modified date, whichever
         file_readers could read) — content["file_date"].
    Returns None if neither is available anywhere — the caller should fall
    back to the processing date as a last resort rather than guessing."""
    return extract_date(content) or (content or {}).get("file_date")


def make_unique_names(items):
    """items: list of (base_name, date_or_None), one per file, in order.
    Returns a list of final, collision-free names in the same order —
    the first file to claim a name gets it plain; later collisions get a
    date suffix if one's available, else an incrementing "(2)", "(3)", ..."""
    seen_count = {}
    used = set()
    final = []
    for base, date_str in items:
        if base not in used and base not in seen_count:
            seen_count[base] = 1
            used.add(base)
            final.append(base)
            continue

        candidate = f"{base} ({date_str})" if date_str else None
        if candidate and candidate not in used:
            used.add(candidate)
            final.append(candidate)
            continue

        seen_count[base] = seen_count.get(base, 1) + 1
        candidate = f"{base} ({seen_count[base]})"
        while candidate in used:
            seen_count[base] += 1
            candidate = f"{base} ({seen_count[base]})"
        used.add(candidate)
        final.append(candidate)
    return final


def _sanitize(name):
    cleaned = ILLEGAL_FILENAME_CHARS.sub("", name).strip()
    return cleaned[:40]


def _name_from_filename(filename):
    stem = Path(filename).stem
    stem = LEADING_REPLY_PREFIX.sub("", stem).strip()

    tokens = re.split(r"[\s_]+", stem)
    first = re.sub(r"[^A-Za-z0-9]", "", tokens[0]) if tokens else ""
    if first and not first.isdigit():
        return first[0].upper() + first[1:]

    # First token wasn't usable (e.g. purely numeric, or empty) — fall back
    # to a cleaned-up version of the whole filename.
    words = re.sub(r"[^A-Za-z0-9]+", " ", stem).split()
    condensed = "".join(w.capitalize() for w in words)[:40]
    return condensed or "Upload"
