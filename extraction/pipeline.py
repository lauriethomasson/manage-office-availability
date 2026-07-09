"""Orchestrates extraction for a batch of uploaded files: read -> try rules
-> fall back to LLM -> normalize. Never raises for a single bad file — each
file gets its own result entry so one failure doesn't sink the batch.

Unlike an earlier version of this module, each file's records stay
separate (one output spreadsheet per source file, not one combined master).
"""
from datetime import date

from .address import spelled_number_to_digits
from .file_readers import read_file
from .geocode import geocode
from .llm_fallback import LLMExtractionError, extract_with_llm
from .naming import extract_date, resolve_provider_name, resolve_source_date
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
        # Prefer the source document's own date (email Date header, or PDF/
        # DOCX metadata) over processing time, so External Ref reflects when
        # the listing was actually sent/dated, not when someone happened to
        # run this batch. Only falls back to today when neither is available.
        ref_date = resolve_source_date(content) or date.today().strftime("%Y-%m-%d")
        external_ref = f"{provider_name}_{ref_date}"
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
    blank, with a clear note printed for whoever's running the batch.

    Also backfills Property Postcode from Nominatim's address breakdown
    when the source text didn't have one (e.g. MetSpace's "9-10 Market
    Place" has no postcode at all) — only as a fallback; a postcode already
    parsed from the source text is never overwritten.

    Lat/Lng/Property Postcode are required fields, so as a last resort —
    when nothing else matched — this retries with nothing but the bare
    building name (+ "London, UK"), dropping any postcode/extra detail
    that may have made the fuller query fail. That bare-name query is
    inherently lower confidence: a name with no house number/street
    (e.g. "Porters Place") isn't guaranteed unique, and confirmed
    empirically that dropping the city/country context entirely can match
    a same-named place on the other side of the world (bare "Porters
    Place" resolves to a street in Barbados). Every match or failure that
    came from this bare-name tier is flagged in the printed log line, and
    it's never used to silently overwrite Property Address 1/Building.
    """
    for record in records:
        query = _geocode_query(record)
        lat, lng, geo_postcode, error = geocode(query)

        # Nominatim can't match a building number spelled out in words
        # (e.g. "Thirty One Alfred Place" for "31 Alfred Place") — if the
        # direct lookup found nothing, retry once with that leading number
        # word converted to digits before giving up.
        if lat is None:
            digit_address = spelled_number_to_digits(record.get("Property Address 1") or "")
            if digit_address:
                retry_query = _geocode_query({**record, "Property Address 1": digit_address})
                retry_lat, retry_lng, retry_postcode, retry_error = geocode(retry_query)
                if retry_lat is not None:
                    query, lat, lng, geo_postcode, error = retry_query, retry_lat, retry_lng, retry_postcode, retry_error

        building = (record.get("Property Address 1") or "").strip()
        bare_query = _geocode_query({"Property Address 1": building}) if building else ""
        # No house number/street in the source at all — a bare building
        # name is inherently a weaker signal than a full street address,
        # regardless of whether this attempt succeeds or fails.
        low_confidence = bool(building) and not any(ch.isdigit() for ch in building)

        if lat is None and bare_query and bare_query != query:
            retry_lat, retry_lng, retry_postcode, retry_error = geocode(bare_query)
            query = bare_query
            if retry_lat is not None:
                lat, lng, geo_postcode, error = retry_lat, retry_lng, retry_postcode, retry_error
            else:
                error = retry_error

        record["Lat"] = lat if lat is not None else ""
        record["Lng"] = lng if lng is not None else ""
        if not record.get("Property Postcode") and geo_postcode:
            record["Property Postcode"] = geo_postcode

        if lat is not None and low_confidence:
            message = (
                f"[geocode] LOW-CONFIDENCE MATCH {filename}: '{query}' — building name only, "
                f"no street/postcode in the source. lat={lat}, lng={lng}. Risk of matching the "
                f"wrong location if this name isn't unique — verify before relying on it."
            )
            _safe_print(message)
        elif error:
            prefix = "[geocode] (low-confidence, building-name-only) " if low_confidence else "[geocode] "
            target = query or building or "(blank)"
            _safe_print(f"{prefix}{filename}: could not geocode '{target}': {error}")


def _safe_print(message):
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
