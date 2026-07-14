"""Orchestrates extraction for a batch of uploaded files: read -> try rules
-> fall back to LLM -> normalize. Never raises for a single bad file — each
file gets its own result entry so one failure doesn't sink the batch.

Unlike an earlier version of this module, each file's records stay
separate (one output spreadsheet per source file, not one combined master).
"""
import re
from datetime import date

from . import memlog, quota
from .address import extract_postcode, spelled_number_to_digits
from .address_lookup import find_address as find_address_via_web_search
from .file_readers import read_file
from .geocode import geocode
from .llm_fallback import LLMExtractionError, extract_with_llm
from .naming import extract_date, resolve_provider_name, resolve_source_date
from .rules import try_rules
from .schema import normalize_record, street_address_only

# A trailing postal district/area code with no inward part (e.g. "W1",
# "SW1Y", "EC2V") — short enough to be a district code, not a full street
# address of its own. Used only to group the SAME building's multiple
# occurrences within one file for _geocode_records' consolidation below,
# never to build a query itself (extraction.geocode's own callers handle
# that; appending a bare district code to a query was confirmed
# empirically to be unreliable — sometimes helps, sometimes breaks an
# otherwise-working match entirely).
_TRAILING_DISTRICT_RE = re.compile(r",?\s*[A-Za-z]{1,2}\d[A-Za-z\d]?\s*$")

# A comma-separated segment that's ENTIRELY just a bare postal district
# (e.g. the "W1" in "5 Swallow Place, W1") — as opposed to a real street
# address (e.g. the "38 Jermyn Street" in "Princes House, 38 Jermyn
# Street"). Used to tell apart the two different reasons a Building
# string can contain a comma when retrying a failed geocode query below.
_BARE_DISTRICT_RE = re.compile(r"^[A-Za-z]{1,2}\d[A-Za-z\d]?$")


def _normalize_building_for_grouping(building):
    stripped = _TRAILING_DISTRICT_RE.sub("", building or "").strip().rstrip(",").strip()
    return stripped.lower()


def _address_retry_candidates(building, digit_address):
    """Ordered fallback address strings to retry when the first geocode
    attempt (this record's own Building text as given) fails — the
    caller tries each in turn, stopping at whichever one succeeds:

    1. digit_address, if given (a spelled-out house number converted to
       digits, e.g. "Thirty One Alfred Place" -> "31 Alfred Place") —
       still a confident full street address, just spelled out.
    2. `building` with a trailing bare postal district (no street of its
       own, e.g. the "W1" in "5 Swallow Place, W1") stripped entirely —
       confirmed empirically that appending a bare district code to a
       query is unreliable (sometimes helps, sometimes breaks an
       otherwise-working match), so this only ever tries WITHOUT one,
       never a query built from the code alone (which was confirmed to
       resolve to some generic district-wide centroid — shared
       identically by every OTHER building that also fell back to it).
    3. The address portion of a "Name, Address[, DistrictCode]" string
       (e.g. "Princes House, 38 Jermyn Street" or, with a district code
       also present, "Princes House, 38 Jermyn Street, SW1Y") —
       confirmed empirically (Crown Estate, 2026-07) that Nominatim
       returns NO MATCH AT ALL for the combined name+address string,
       even though the address portion alone resolves correctly every
       time it was tested — tried both with and without a trailing bare
       district code of its own, since that combination was also
       confirmed to sometimes fail where the bare address alone
       succeeds."""
    seen = set()
    candidates = []

    def add(value):
        value = (value or "").strip().strip(",").strip()
        if value and value not in seen:
            seen.add(value)
            candidates.append(value)

    if digit_address:
        add(digit_address)

    no_district = _TRAILING_DISTRICT_RE.sub("", building or "").strip()
    add(no_district)

    for base in (building or "", no_district):
        if "," not in base:
            continue
        before_comma, after_comma = (p.strip() for p in base.split(",", 1))
        if _BARE_DISTRICT_RE.match(after_comma):
            add(before_comma)
        else:
            add(after_comma)
            add(_TRAILING_DISTRICT_RE.sub("", after_comma).strip())

    return candidates


def process_files(paths):
    """Returns a list of per-file dicts:
    {filename, status: "ok"|"error", method: "rule:<Name>"|"llm"|None,
     records, record_count, error, warning, provider_name, date}
    provider_name/date/records are only meaningful when status == "ok".
    warning is set alongside a normal "ok" status/None error — either or
    both of: a PDF bigger than what's actually been tested end-to-end
    (extraction.file_readers.TESTED_MAX_PDF_PAGES/TESTED_MAX_PDF_BYTES —
    distinct from that module's own hard MAX_PDF_PAGES ceiling, which is
    a real error instead), and/or a file that extracted successfully but
    hit Gemini's daily quota partway through its own address-lookup
    fallback (some rows' Lat/Lng/postcode fell back further than usual,
    to a less reliable bare-name Nominatim match) — neither is something
    that failed the file itself.
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
            "warning": None,
            "provider_name": None,
            "date": None,
            "email_html": None,
            "pages_text": None,
        }
        memlog.log("before file parsing", filename)
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
        memlog.log("after file parsing", filename)

        # Set as soon as it's known (rather than only at the very end)
        # so a later warning — e.g. Gemini quota exhaustion, below — can
        # append to it instead of clobbering it; see extraction.
        # file_readers.TESTED_MAX_PDF_PAGES/TESTED_MAX_PDF_BYTES for what
        # this is actually based on (a real, repeatedly-tested figure,
        # not a guess) and why it's a warning, not an error, unlike the
        # separate hard MAX_PDF_PAGES ceiling.
        if content.get("size_warning"):
            result["warning"] = content["size_warning"]

        # An .eml's own HTML body (already parsed by file_readers, not
        # re-rendered) — lets app.py link Link to File at that HTML
        # directly instead of the raw .eml, so it opens in-browser with its
        # original images (the markup already points at the sender's
        # hosted image URLs) rather than downloading a mail file. Falls
        # back to None for a plain-text-only email, or anything else.
        if path.suffix.lower() == ".eml" and content.get("html"):
            result["email_html"] = content["html"]

        # Per-page PDF text, so app.py's Floor Plan/High Res Images
        # enrichment (extraction.pdf_images) can tell which source page a
        # given LLM-extracted listing came from — None for anything that
        # isn't a PDF (an .eml/table-based source has no "pages" concept).
        if path.suffix.lower() == ".pdf" and content.get("pages_text"):
            result["pages_text"] = content["pages_text"]

        rule_name, raw_records = try_rules(content)
        llm_source_name = None
        if raw_records:
            result["method"] = f"rule:{rule_name}"
        else:
            memlog.log("before LLM call", filename)
            try:
                raw_records, llm_source_name = extract_with_llm(content["text"], source_hint=filename)
                result["method"] = "llm"
            except LLMExtractionError as e:
                memlog.log("after LLM call (raised LLMExtractionError)", filename)
                result["status"] = "error"
                result["error"] = str(e)
                results.append(result)
                continue
            except Exception as e:
                memlog.log("after LLM call (raised unexpected exception)", filename)
                result["status"] = "error"
                result["error"] = f"Unexpected error during LLM extraction: {e}"
                results.append(result)
                continue
            memlog.log("after LLM call", filename)

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
        quota_exhausted = _geocode_records(normalized, filename, provider_name)

        # Deliberately AFTER geocoding, not before: _geocode_records (and
        # everything it calls — _geocode_query, _address_retry_candidates,
        # the is_bare_name web-search branch) reads Property Address 1 as
        # the full "Name, Street, City Postcode" text straight from
        # Building, exactly as it always has — that's what its own retry
        # logic was built around (a combined name+address string can
        # confuse Nominatim; see this module's docstring). Only now, once
        # nothing further needs that fuller text, is Property Address 1
        # overwritten with a clean street-only value for the actual
        # spreadsheet output — Building itself is never touched.
        for record in normalized:
            record["Property Address 1"] = street_address_only(record.get("Building"))

        if quota_exhausted:
            # Scoped to this file's own note, not a batch-wide error — the
            # file's records extracted fine; this only affects rows whose
            # address had to fall back to the web-search tier and hit the
            # daily limit there specifically (a plain building name with
            # no street/postcode in the source at all — see
            # _geocode_records below). Appended, not overwritten — a
            # large/untested-size warning may already be set above, and a
            # file can genuinely have both going on at once.
            quota_note = (
                quota.reset_message("Gemini's daily address-search limit")
                + " Some rows' Property Address/Postcode/Lat/Lng fell back to a plain "
                "building-name lookup, which is less reliable — worth a manual check "
                'for any row marked "(Not in source text)".'
            )
            result["warning"] = f"{result['warning']} {quota_note}" if result["warning"] else quota_note

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

    # Mirroring both on-disk lookup caches to durable storage (once per
    # batch, not once per record — see _save_cache in each module) used to
    # happen synchronously right here. Confirmed via Render's own logs that
    # a worker was once killed while stuck inside exactly this call —
    # a real network round-trip to B2/S3 that can run long — which a
    # generic SIGKILL then gets misreported as "Perhaps out of memory?"
    # regardless of the real cause. Moved to the same background-thread
    # pattern app.py already uses for every other storage.upload call
    # (see app.py's _flush_caches, started right after this function
    # returns) so it can never block the HTTP response or contribute to a
    # worker timeout.
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

    Returns True if the web-search tier hit Gemini's daily quota limit
    for at least one record in this file — process_files turns this into
    a per-file "warning" (not "error"; the records themselves still
    extracted fine) so a batch that had to fall back further than usual
    is explained rather than silently degraded.
    """
    # When the SAME building appears more than once in this file with
    # different amounts of qualifying detail (e.g. Crown Estate's "1 Vine
    # Street, W1" for one floor vs a plain "1 Vine Street" for a different
    # floor elsewhere in the same document — confirmed the source PDF
    # itself just doesn't repeat the area code in every section), geocode
    # every occurrence using whichever text is richest/most qualified.
    # Confirmed empirically that the bare version alone can resolve
    # confidently to a coincidentally-real but wrong address (Walthamstow,
    # ~12km from the real Mayfair one) with no second Nominatim candidate
    # for extraction.geocode's own ambiguity check to catch, while the
    # qualified version resolves correctly every time.
    richest_building = {}
    for record in records:
        building = (record.get("Property Address 1") or "").strip()
        if not building:
            continue
        base = _normalize_building_for_grouping(building)
        if base not in richest_building or len(building) > len(richest_building[base]):
            richest_building[base] = building

    quota_exhausted = False
    for record in records:
        building = (record.get("Property Address 1") or "").strip()
        has_digit = any(ch.isdigit() for ch in building)
        # Nominatim can't match a building number spelled out in words
        # (e.g. "Thirty One Alfred Place" for "31 Alfred Place") — this
        # still counts as a confident full street address, just spelled
        # out, not the bare-name case below.
        digit_address = spelled_number_to_digits(building) if building and not has_digit else None
        is_bare_name = bool(building) and not has_digit and not digit_address

        # The record actually asked about below always keeps its own
        # Building/Property Address 1 text — this only substitutes a
        # richer sibling's text for the *query* sent to the geocoder.
        query_source = record
        if building and not is_bare_name:
            richer = richest_building.get(_normalize_building_for_grouping(building))
            if richer and richer != building:
                query_source = {**record, "Property Address 1": richer}

        query = _geocode_query(query_source)
        derived_note = False
        sources = []

        if is_bare_name:
            lat = lng = geo_postcode = None
            error = None
            web_address, web_sources, hit_quota = find_address_via_web_search(building, provider_name)
            if hit_quota:
                quota_exhausted = True
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
                # still flagged if it does find something. confident=False
                # so this specific result is never trusted from cache on a
                # future run (extraction.geocode.geocode) — otherwise a
                # run where the web-search tier fails for a transient
                # reason (e.g. quota exhaustion) permanently poisons the
                # cache with this tier's own coincidental-match risk,
                # exactly like the bug this same safeguard already fixed
                # once for extraction.address_lookup's own cache.
                lat, lng, geo_postcode, error = geocode(query, confident=False)
                if lat is not None:
                    derived_note = True
        else:
            lat, lng, geo_postcode, error = geocode(query)
            if lat is None:
                for candidate_address in _address_retry_candidates(building, digit_address):
                    retry_query = _geocode_query({**record, "Property Address 1": candidate_address})
                    if retry_query == query:
                        continue
                    retry_lat, retry_lng, retry_postcode, retry_error = geocode(retry_query)
                    if retry_lat is not None:
                        query, lat, lng, geo_postcode, error = retry_query, retry_lat, retry_lng, retry_postcode, retry_error
                        break

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

    return quota_exhausted


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
