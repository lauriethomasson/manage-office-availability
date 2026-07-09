"""Helpers for deriving a Kato-compatible postcode from freeform address text."""
import re

UK_POSTCODE_RE = re.compile(r"\b([A-Za-z]{1,2}\d[A-Za-z\d]?\s*\d[A-Za-z]{2})\b")


def extract_postcode(text):
    """Return a normalized UK postcode (e.g. "EC3A 7JB") found in `text`, or
    "" if none is confidently present. Never guesses — a near-miss is left
    blank rather than returned as a best effort."""
    if not text:
        return ""
    match = UK_POSTCODE_RE.search(text)
    if not match:
        return ""
    compact = match.group(1).upper().replace(" ", "")
    if len(compact) < 5:
        return ""
    return f"{compact[:-3]} {compact[-3:]}"
