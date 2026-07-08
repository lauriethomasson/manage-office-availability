"""Target schema for the consolidated master spreadsheet.

Derived from the example output (Kitt's Availability PDF): columns, ordering,
and the PCM/PSF relationship used across all three example sources.
"""

# Column order for the master spreadsheet — must match the example output.
COLUMNS = [
    "Area",
    "Building",
    "Floor/Unit",
    "Size (sq ft)",
    "Desks (max)",
    "Marketing Price (Based on Min Term) PCM",
    "Marketing Price (Based on Min Term) PSF",
    "Link to Brochure",
    "Min. Term",
    "Special Features",
    "State of Space",
    "Legal Structure",
    "Broker Fee",
    "Contact 1",
    "Contact 2",
    "Floor Plan",
    "High Res Images",
]

# Fields the LLM fallback must return JSON for (same as COLUMNS — kept as a
# separate name since the LLM prompt refers to "fields", not spreadsheet
# columns, but they're identical today).
LLM_FIELDS = list(COLUMNS)


def blank_record():
    return {c: "" for c in COLUMNS}


def normalize_record(record):
    """Fill in any missing columns, coerce numeric columns, and derive
    PCM/PSF from each other when only one is present (all three example
    sources confirm PSF == PCM * 12 / size_sqft, annualized)."""
    out = blank_record()
    for c in COLUMNS:
        v = record.get(c, "")
        out[c] = "" if v is None else v

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

    return out


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
