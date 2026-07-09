"""Orchestrates extraction for a batch of uploaded files: read -> try rules
-> fall back to LLM -> normalize. Never raises for a single bad file — each
file gets its own result entry so one failure doesn't sink the batch.

Unlike an earlier version of this module, each file's records stay
separate (one output spreadsheet per source file, not one combined master).
"""
from datetime import date

from .file_readers import read_file
from .geocode import geocode
from .llm_fallback import LLMExtractionError, extract_with_llm
from .naming import extract_date, resolve_provider_name
from .rules import try_rules
from .schema import normalize_record


def process_files(paths):
    """Returns a list of per-file dicts:
    {filename, status: "ok"|"error", method: "rule:<Name>"|"llm"|None,
     records, record_count, error, provider_name, date}
    provider_name/date/records are only meaningful when status == "ok".
    """
    results = []

    for path in paths:
        filename = path.name if hasattr(path, "name") else str(path)
        result = {
            "filename": filename,
            "status": "ok",
            "method": None,
            "records": [],
            "record_count": 0,
            "error": None,
            "provider_name": None,
            "date": None,
        }
        try:
            content = read_file(path)
        except ValueError as e:
            result["status"] = "error"
            result["error"] = str(e)
            results.append(result)
            continue
        except Exception as e:
            result["status"] = "error"
            result["error"] = f"Unexpected error reading file: {e}"
            results.append(result)
            continue

        rule_name, raw_records = try_rules(content)
        llm_source_name = None
        if raw_records:
            result["method"] = f"rule:{rule_name}"
        else:
            try:
                raw_records, llm_source_name = extract_with_llm(content["text"], source_hint=filename)
                result["method"] = "llm"
            except LLMExtractionError as e:
                result["status"] = "error"
                result["error"] = str(e)
                results.append(result)
                continue
            except Exception as e:
                result["status"] = "error"
                result["error"] = f"Unexpected error during LLM extraction: {e}"
                results.append(result)
                continue

        normalized = [normalize_record(r) for r in raw_records]
        normalized = [r for r in normalized if r.get("Building") or r.get("Area")]
        if not normalized:
            result["status"] = "error"
            result["error"] = "No usable records found in this file"
            results.append(result)
            continue

        _geocode_records(normalized, filename)

        provider_name = resolve_provider_name(rule_name, filename, llm_source_name)
        external_ref = f"{provider_name}_{date.today().strftime('%Y-%m-%d')}"
        for record in normalized:
            record["External Ref"] = external_ref

        result["records"] = normalized
        result["record_count"] = len(normalized)
        result["provider_name"] = provider_name
        result["date"] = extract_date(content)
        results.append(result)

    return results


def _geocode_records(records, filename):
    """Fills Lat/Lng in place for each record via extraction.geocode.
    geocode() caches on disk by address string, so repeat buildings (e.g.
    several floors in the same Knotel building) cost one lookup, not one
    per row. Failures are never fatal to the row — Lat/Lng are just left
    blank, with a clear note printed for whoever's running the batch."""
    for record in records:
        query = _geocode_query(record)
        lat, lng, error = geocode(query)
        record["Lat"] = lat if lat is not None else ""
        record["Lng"] = lng if lng is not None else ""
        if error:
            message = f"[geocode] {filename}: could not geocode '{query or record.get('Property Address 1') or '(blank)'}': {error}"
            try:
                print(message)
            except UnicodeEncodeError:
                print(message.encode("ascii", "replace").decode("ascii"))


def _geocode_query(record):
    """All current sources are London office listings, but the extracted
    address text is often just a building name/street (e.g. "28 Bruton
    Street") with no postcode or city — so append London/UK context for
    the geocoder rather than passing an ambiguous bare address. This only
    shapes the search query; it never fabricates the coordinates
    themselves, and Property Address 1/Property Postcode in the output are
    untouched.

    Deliberately does NOT add the informal "Area" field (e.g. "West End",
    "Fitzrovia") — these are marketing/neighbourhood names, not formal
    localities, and confirmed empirically to make Nominatim's free-text
    matching fail on otherwise-correct addresses (e.g. "9-10 Market Place,
    West End, London, UK" -> no match, but "9-10 Market Place, London, UK"
    matches correctly)."""
    address = (record.get("Property Address 1") or "").strip()
    if not address:
        return ""

    postcode = (record.get("Property Postcode") or "").strip()
    if postcode and postcode not in address:
        address = f"{address}, {postcode}"

    if "london" not in address.lower():
        address = f"{address}, London, UK"

    return address
