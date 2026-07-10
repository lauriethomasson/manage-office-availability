"""Target schema for the consolidated master spreadsheet.

Derived from the example output (Kitt's Availability PDF): columns, ordering,
and the PCM/PSF relationship used across all three example sources.
"""
from .address import extract_postcode

# Fields a rule parser or the LLM fallback actually extracts from a source
# document — must match the example output.
SOURCE_FIELDS = [
    "Area",
    "Building",
    "Floor/Unit",
    "Size (sq ft)",
    "Desks (max)",
    "Marketing Price (Based on Min Term) PCM",
    "Marketing Price (Based on Min Term) PSF",
    "Link to File",
    "Min. Term",
    "Special Features",
    "State of Space",
    "Legal Structure",
    "Broker Fee",
    "Contacts",
    "Floor Plan",
    "High Res Images",
]

# Extra columns required for Kato bulk-upload compatibility (see the
# "Loader" sheet of kato-disposals-loader-example-LATEST VERSION (3).xlsx).
# These are derived/defaulted in normalize_record, never extracted directly
# from a source document.
KATO_FIELDS = [
    "External Ref",
    "Assigned Agents",
    "Property Address 1",
    "Property Postcode",
    "Lat",
    "Lng",
    "For Sale",
    "To Let",
]

# Column order for the master spreadsheet — Kato columns (External Ref
# first) lead, followed by the extracted source columns.
COLUMNS = KATO_FIELDS + SOURCE_FIELDS

# Fields the LLM fallback must return JSON for — only the ones actually
# present in source documents. The Kato fields are derived afterwards by
# normalize_record, not invented by the LLM (that would risk hallucinated
# postcodes/coordinates).
LLM_FIELDS = list(SOURCE_FIELDS)


def blank_record():
    return {c: "" for c in COLUMNS}


def normalize_record(record):
    """Fill in any missing columns, coerce numeric columns, derive PCM/PSF
    from each other when only one is present (all three example sources
    confirm PSF == PCM * 12 / size_sqft, annualized), and derive the Kato
    bulk-upload columns from the fields actually extracted above.

    Lat/Lng are deliberately left blank here — geocoding is a network call
    and is done as a separate step (see extraction.geocode), keyed off the
    Property Address 1 / Property Postcode this function produces.
    """
    out = blank_record()
    for c in SOURCE_FIELDS:
        v = record.get(c, "")
        out[c] = "" if v is None else v

    # Underscore-prefixed keys are a rule's own staging fields for a later
    # pipeline/app.py step (e.g. gpe.py's "_high_res_candidates", resolved
    # into a real High Res Images URL/gallery by app.py's
    # _finalize_high_res_images after normalization) — not part of the
    # output schema itself, so they're not in SOURCE_FIELDS/blank_record,
    # but need to survive this rebuild rather than being silently dropped.
    for k, v in record.items():
        if k.startswith("_"):
            out[k] = v

    out["Size (sq ft)"] = _to_number(out["Size (sq ft)"])
    out["Desks (max)"] = _to_number(out["Desks (max)"])
    out["Marketing Price (Based on Min Term) PCM"] = _to_number(out["Marketing Price (Based on Min Term) PCM"])
    out["Marketing Price (Based on Min Term) PSF"] = _to_number(out["Marketing Price (Based on Min Term) PSF"])

    size = out["Size (sq ft)"]
    pcm = out["Marketing Price (Based on Min Term) PCM"]
    psf = out["Marketing Price (Based on Min Term) PSF"]

    if size:
        if pcm and not psf:
            out["Marketing Price (Based on Min Term) PSF"] = round(pcm * 12 / size, 2)
        elif psf and not pcm:
            out["Marketing Price (Based on Min Term) PCM"] = round(psf * size / 12, 2)

    # External Ref identifies the whole source batch (same value for every
    # row in a spreadsheet), not an individual listing — it's stamped in
    # by extraction.pipeline.process_files once the provider name and
    # processing date are known, not derivable per-record here.
    out["External Ref"] = ""
    # Required field — a source with no contact/agent info at all (no
    # Contacts value to mirror) would otherwise leave this blank.
    out["Assigned Agents"] = out["Contacts"] or "Unknown"
    out["Property Address 1"] = out["Building"]
    out["Property Postcode"] = extract_postcode(out["Building"])
    out["Lat"] = ""
    out["Lng"] = ""
    # "Sale Price" isn't a spreadsheet column — it's a raw per-listing
    # signal some sources provide (e.g. BC lists a separate Sale Price
    # alongside its rental price for a handful of listings) that a rule
    # parser (extraction/rules/grid.py) or the LLM fallback may set on the
    # raw record. For Sale reflects whether that signal is a genuine value,
    # not "N/A"/blank — "No" whenever a source has no such signal at all,
    # which is every current source except a few BC listings.
    out["For Sale"] = "Yes" if _has_real_value(record.get("Sale Price")) else "No"
    out["To Let"] = "Yes"

    return out


_NO_VALUE_TOKENS = {"", "n/a", "na", "-", "none", "tbc", "0"}


def _has_real_value(v):
    if v is None:
        return False
    return str(v).strip().lower() not in _NO_VALUE_TOKENS


def _to_number(v):
    if v == "" or v is None:
        return ""
    if isinstance(v, (int, float)):
        return v
    s = str(v).strip()
    s = s.replace("£", "").replace(",", "").strip()
    if not s:
        return ""
    try:
        if "." in s:
            return float(s)
        return int(s)
    except ValueError:
        try:
            return float(s)
        except ValueError:
            return ""
