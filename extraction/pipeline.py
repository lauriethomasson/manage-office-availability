"""Orchestrates extraction for a batch of uploaded files: read -> try rules
-> fall back to LLM -> normalize. Never raises for a single bad file — each
file gets its own result entry so one failure doesn't sink the batch.

Unlike an earlier version of this module, each file's records stay
separate (one output spreadsheet per source file, not one combined master).
"""
from datetime import date

from .address import extract_postcode, spelled_number_to_digits
from .address_lookup import find_address as find_address_via_web_search
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
            "email_html": None,
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

        # An .eml's own HTML body (already parsed by file_readers, not
        # re-rendered) — lets app.py link Link to Brochure at that HTML
        # directly instead of the raw .eml, so it opens in-browser with its
        # original images (the markup already points at the sender's
        # hosted image URLs) rather than downloading a mail file. Falls
        # back to None for a plain-text-only email, or anything else.
        if path.suffix.lower() == ".eml" and content.get("html"):
            result["email_html"] = content["html"]

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

        # Resolved before geocoding (rather than after, as before) so the
        # web-search fallback can pass it along as disambiguating context
        # (e.g. "Elsley GPE Fully Managed" instead of just "Elsley").
        provider_name = resolve_provider_name(rule_name, filename, llm_source_name)
        _geocode_records(normalized, filename, provider_name)

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


def _geocode_records(records, filename, provider_name):
    """Fills Lat/Lng in place for each record via extraction.geocode.
    geocode() caches on disk by address string, so repeat buildings (e.g.
    several floors in the same Knotel building) cost one lookup, not one
    per row. Failures are never fatal to the row — Lat/Lng are just left
    blank, with a clear note printed for whoever's running the batch.

    Also backfills Property Postcode from Nominatim's address breakdown
    when the source text didn't have one (e.g. MetSpace's "9-10 Market
    Place" has no postcode at all) — only as a fallback; a postcode already
    parsed from the source text is never overwritten.

    Lat/Lng/Property Postcode are required fields. When the source gives
    nothing but a bare building name (no street/house number at all — not
    even one spelled out in words) there's a real risk of geocoding it to
    the wrong place entirely: a bare-name Nominatim search for "Porters
    Place" alone matched a street in Barbados, and "Elsley" alone matched a
    building in Battersea (SW11) when the real GPE-managed "Elsley" is in
    Fitzrovia (W1W) — and critically, Nominatim returned a match either
    way, so this can't be caught by "retry only if the direct lookup found
    nothing." For a genuinely bare name, this never trusts a direct/bare
    Nominatim match as the primary result at all — it tries an actual web
    search FIRST, Gemini + Google Search grounding
    (extraction.address_lookup), with the source/provider name included as
    disambiguating context (e.g. "Elsley GPE Fully Managed" instead of
    just "Elsley"). Only if that finds nothing does this fall back to a
    plain bare-name Nominatim search, still exposed to the same risk.
    Either way, the result is marked "(Not in source text)" in the output,
    since it reflects a real gap in the source document, not a
    wrong-vs-right judgment on the value itself — it's never used to
    silently overwrite Property Address 1/Building.
    """
    for record in records:
        building = (record.get("Property Address 1") or "").strip()
        has_digit = any(ch.isdigit() for ch in building)
        # Nominatim can't match a building number spelled out in words
        # (e.g. "Thirty One Alfred Place" for "31 Alfred Place") — this
        # still counts as a confident full street address, just spelled
        # out, not the bare-name case below.
        digit_address = spelled_number_to_digits(building) if building and not has_digit else None
        is_bare_name = bool(building) and not has_digit and not digit_address

        query = _geocode_query(record)
        derived_note = False
        sources = []

        if is_bare_name:
            lat = lng = geo_postcode = None
            error = None
            web_address, web_sources = find_address_via_web_search(building, provider_name)
            if web_address:
                web_query = _geocode_query({"Property Address 1": web_address})
                lat, lng, geo_postcode, error = geocode(web_query)
                if lat is not None:
                    query = web_query
                    # Prefer the postcode actually present in the found
                    # address text over Nominatim's address-breakdown
                    # postcode — confirmed empirically that Nominatim can
                    # tag a wide building polygon with a different, coarser
                    # postcode than the specific address searched for (e.g.
                    # "11 St John Street" geocodes to a building spanning
                    # house numbers 11-33, whose OSM postcode, EC1M 4NX,
                    # doesn't match number 11's real postcode).
                    geo_postcode = extract_postcode(web_address) or geo_postcode
                    derived_note = True
                    sources = web_sources

            if lat is None:
                # Web search found nothing confident enough (unconfigured,
                # not enough independent sources, or genuinely not found)
                # — last resort: Nominatim on just the bare name. Same
                # risk as before (a coincidental match elsewhere), so
                # still flagged if it does find something.
                lat, lng, geo_postcode, error = geocode(query)
                if lat is not None:
                    derived_note = True
        else:
            lat, lng, geo_postcode, error = geocode(query)
            if lat is None and digit_address:
                retry_query = _geocode_query({**record, "Property Address 1": digit_address})
                retry_lat, retry_lng, retry_postcode, retry_error = geocode(retry_query)
                if retry_lat is not None:
                    query, lat, lng, geo_postcode, error = retry_query, retry_lat, retry_lng, retry_postcode, retry_error

        postcode_from_geocode = False
        if not record.get("Property Postcode") and geo_postcode:
            record["Property Postcode"] = geo_postcode
            postcode_from_geocode = True

        if lat is not None:
            if derived_note:
                # The source gave nothing but a bare building name — this
                # value was derived (web search or a bare-name geocode),
                # not read directly from the source document. Flagged in
                # the spreadsheet itself, not just the console log, so
                # it's distinguishable at a glance. Property Postcode only
                # gets the same marker when it came from this same lookup —
                # a postcode already present in the source text is left
                # alone, since it isn't in question.
                record["Lat"] = f"{lat} (Not in source text)"
                record["Lng"] = f"{lng} (Not in source text)"
                if postcode_from_geocode:
                    record["Property Postcode"] = f"{geo_postcode} (Not in source text)"
                # Surfaced in the spreadsheet too (spreadsheet.write_xlsx
                # attaches this as a cell comment on Lat) — so a wrong
                # answer is traceable to what it was actually based on,
                # not just an opaque coordinate.
                if sources:
                    record["_geocode_sources"] = sources
                sources_note = f" Sources: {'; '.join(sources)}." if sources else ""
                _safe_print(
                    f"[geocode] (Not in source text) {filename}: '{building}' -> '{query}': "
                    f"lat={lat}, lng={lng}.{sources_note} No street/postcode in the source — verify before relying on it."
                )
            else:
                record["Lat"] = lat
                record["Lng"] = lng
        else:
            # Required fields — flag directly in the cell, not just the
            # console log, so a genuine geocoding gap is visible to anyone
            # opening the spreadsheet, distinguishable from a field that's
            # blank for some other reason.
            record["Lat"] = "Needs manual lookup"
            record["Lng"] = "Needs manual lookup"
            if not record.get("Property Postcode"):
                record["Property Postcode"] = "Needs manual lookup"
            prefix = "[geocode] (bare building name, no match found) " if is_bare_name else "[geocode] "
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
