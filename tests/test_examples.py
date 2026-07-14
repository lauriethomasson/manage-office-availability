"""Regression test against the 3 example files this app was built for.
Run with: python tests/test_examples.py
Asserts on rule *names* and minimum record counts (not exact field values —
those are covered by manual review) so this catches "a parser stopped
matching" or "extraction silently dropped most rows" regressions.
"""
import hashlib
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from extraction.file_readers import read_file
from extraction.rules import try_rules
from extraction.schema import normalize_record, street_address_only, names_only
from extraction import geocode as geocode_module
from extraction import pdf_images
import app as app_module

EXPECTATIONS = [
    ("Fw_ Knotel Availability _ 30_06_2026.eml", "Knotel", 16),
    ("Fw_ Knotel Availability _ 13_07_2026.eml", "Knotel", 15),
    ("Fw_ MetSpace Availability Update.eml", "MetSpace", 14),
    ("Fw_ The latest GPE Fully Managed availability – workspaces you won't want to miss..eml", "GPE", 15),
    # 57, not 19 — a real, previously-shipped regression (2026-07 audit):
    # extraction.rules.grid only ever looked at the FIRST table matching
    # its header keywords, so a long table split across multiple
    # page-tables by pdfplumber (confirmed: this exact PDF has 3, each
    # repeating the same header row) silently dropped every row on every
    # table after the first — 38 of 57 real listings, across 2 entirely-
    # ignored tables. An exact count (not just ">= 19", which the old,
    # already-broken 19 would have kept passing) is what actually catches
    # this class of bug — see main()'s own exact-match check below.
    ("Kitt's Availability (External) - Live Availability.pdf", "Grid/Tabular", 57),
    ("BC Current Availability.pdf", "BC", 11),
    ("John Stow House.pdf", "Breezblok", 1),
]


def check_metspace_floor_plans(failures):
    """Targeted regression test for two real bugs that already shipped:
    MetSpace's rule originally never extracted Floor Plan/High Res Images
    at all, despite the source email genuinely containing a per-listing
    floor plan image (confirmed by actually viewing several of them - real
    floor-plan diagrams, not building photos, which is why this checks
    Floor Plan specifically and not High Res Images). The fix for that was
    then found to have the image-to-listing direction backwards (assumed
    "image precedes link", inherited from Knotel's pattern, when this
    source's own raw HTML is actually "image follows link" - the same
    class of off-by-one as GPE's High Res Images bug) - confirmed by
    walking the raw html_items sequence directly, which is why every one
    of the 14 listings has a real, distinct floor plan image, not 13/14.
    Pins the exact, known-correct count for this specific example file
    rather than a vague ">0" check, so a future regression (e.g. someone
    "fixing" the html_items image filter, or re-flipping the direction,
    and breaking this again) fails loudly here instead of only being
    caught by manually spot-checking a spreadsheet later.

    Also pins Brochure PDF (2026-07 audit): each building name in this
    source is itself the hyperlink to a real, listing-specific brochure
    (confirmed by actually following one — a Mailchimp click-tracking
    redirect that 302s to a Google Drive file literally titled "9-10
    Market Place - 2nd Floor") — reuses the exact same matched-link
    position that Floor Plan's own image lookup already relies on, so
    this also guards against that position tracking regressing (all 14
    rows, including the 5 same-building "141 Fenchurch Street (Monument)"
    floors, must each get their own distinct link, not a neighbour's)."""
    filename = "Fw_ MetSpace Availability Update.eml"
    path = ROOT / filename
    if not path.exists():
        failures.append(f"{filename}: example file not found (expected at {path})")
        return

    content = read_file(path)
    rule_name, records = try_rules(content)
    if rule_name != "MetSpace" or not records:
        failures.append(f"{filename}: expected rule 'MetSpace' with records, got '{rule_name}'")
        return

    floor_plan_count = sum(1 for r in records if (r.get("Floor Plan") or "").strip())
    high_res_count = sum(1 for r in records if (r.get("High Res Images") or "").strip())
    distinct_floor_plans = len({r["Floor Plan"] for r in records if (r.get("Floor Plan") or "").strip()})

    # Known-correct for this exact example file: all 14 listings have a
    # real, distinct floor plan image — confirmed directly against the raw
    # HTML (each listing's link is immediately followed by its own image).
    if floor_plan_count != 14:
        failures.append(
            f"{filename}: expected all 14 records to have a real Floor Plan URL, got {floor_plan_count}/{len(records)} "
            "— MetSpace's floor-plan-image extraction may be broken again"
        )
    if distinct_floor_plans != floor_plan_count:
        failures.append(
            f"{filename}: expected every populated Floor Plan to be distinct, got only {distinct_floor_plans} distinct "
            f"URLs across {floor_plan_count} populated rows — a listing may be getting a neighbor's image "
            "(the image-follows-link direction may have regressed to image-precedes-link)"
        )
    # High Res Images should stay blank for MetSpace: the only embedded
    # image per listing in this source is a floor plan, not a photo -
    # populating this too would be fabricating a distinction the source
    # doesn't actually have.
    if high_res_count != 0:
        failures.append(
            f"{filename}: expected High Res Images blank for all rows (MetSpace's only per-listing image is a "
            f"floor plan, not a photo), got {high_res_count}/{len(records)} populated"
        )

    brochure_count = sum(1 for r in records if (r.get("Brochure PDF") or "").strip())
    distinct_brochures = len({r["Brochure PDF"] for r in records if (r.get("Brochure PDF") or "").strip()})
    if brochure_count != 14:
        failures.append(
            f"{filename}: expected all 14 records to have a real Brochure PDF URL, got {brochure_count}/{len(records)} "
            "— MetSpace's brochure-link extraction may be broken"
        )
    if distinct_brochures != brochure_count:
        failures.append(
            f"{filename}: expected every populated Brochure PDF to be distinct, got only {distinct_brochures} distinct "
            f"URLs across {brochure_count} populated rows — a listing may be getting a neighbour's brochure link"
        )
    by_building_floor = {(r.get("Building"), r.get("Floor/Unit")): r.get("Brochure PDF") for r in records}
    expected_brochure = {
        ("9-10 Market Place", ""): "https://us.list-manage.com/Q6gnhZazI8v?e=095cb98613&c2id=a206dcecbc185bd9a5d5a46b47a3996f",
        ("141 Fenchurch Street (Monument)", "G Floor"): "https://us.list-manage.com/WrtQM3D1UAI?e=095cb98613&c2id=a206dcecbc185bd9a5d5a46b47a3996f",
        ("141 Fenchurch Street (Monument)", "7th Floor"): "https://us.list-manage.com/1Cu5cqfBviI?e=095cb98613&c2id=a206dcecbc185bd9a5d5a46b47a3996f",
    }
    for key, expected_url in expected_brochure.items():
        actual = by_building_floor.get(key)
        if actual != expected_url:
            failures.append(f"{filename}: {key} expected Brochure PDF {expected_url!r}, got {actual!r}")

    if floor_plan_count == 14 and distinct_floor_plans == 14 and high_res_count == 0 and brochure_count == 14 and distinct_brochures == 14:
        print(
            f"OK  {filename}: Floor Plan and Brochure PDF each populated with {distinct_floor_plans}/{distinct_brochures} "
            f"distinct URLs for all {len(records)} rows, High Res Images correctly blank"
        )


def check_gpe_high_res_images(failures):
    """Targeted regression test for a real bug that already shipped
    *twice*, plus a real assumption that turned out wrong a third time:
    first, GPE's rule never extracted High Res Images at all, despite the
    source email genuinely containing real per-building marketing photos
    (confirmed by actually viewing several - real building photos, unlike
    MetSpace's, which is why this checks High Res Images specifically and
    not Floor Plan). Then the fix itself had an off-by-one: it attributed
    each building's real photo to the *next* building instead, which a
    naive ">= N populated" count entirely failed to catch, since 11 of 15
    rows were still "populated" — just with the wrong photo. Then a third
    bug: the fix assumed every building has exactly one photo shared
    across all its floors, but visual confirmation against the actual
    email showed some buildings (those also featured in the promotional
    blurbs earlier in the email, not just their own "CURRENT AVAILABILITY"
    listing card) genuinely have TWO distinct real photos, not one.

    This rule now stashes candidate photo URLs on "_high_res_candidates"
    (app.py turns 1 candidate into a direct High Res Images link, 2+ into
    a small gallery page — see _finalize_high_res_images) rather than
    setting High Res Images itself, so this checks the candidates
    directly rather than routing through app.py's finalizer. It checks
    actual per-building correctness, not just a count: every distinct
    building must map to its own distinct candidate set (no collisions
    between buildings), and the known-correct counts for this exact
    example file — re-verified by actually viewing every photo in the
    source email — must hold exactly."""
    filename = "Fw_ The latest GPE Fully Managed availability – workspaces you won't want to miss..eml"
    path = ROOT / filename
    if not path.exists():
        failures.append(f"{filename}: example file not found (expected at {path})")
        return

    content = read_file(path)
    rule_name, records = try_rules(content)
    if rule_name != "GPE" or not records:
        failures.append(f"{filename}: expected rule 'GPE' with records, got '{rule_name}'")
        return

    floor_plan_count = sum(1 for r in records if (r.get("Floor Plan") or "").strip())
    # Floor Plan should stay blank for GPE: no separate floor-plan-labeled
    # image or link exists anywhere in this source - populating it would
    # be fabricating a distinction the source doesn't actually have.
    if floor_plan_count != 0:
        failures.append(
            f"{filename}: expected Floor Plan blank for all rows (no separate floor-plan resource exists in "
            f"GPE's source), got {floor_plan_count}/{len(records)} populated"
        )

    # Known-correct for this exact example file, re-verified by actually
    # viewing every photo in the source email: 5 buildings have exactly 1
    # real photo (their own "CURRENT AVAILABILITY" listing-card photo),
    # 4 buildings (also featured in the promotional blurbs earlier in the
    # email) have exactly 2 distinct real photos - 13 distinct images
    # total, none missing, none duplicated across buildings.
    EXPECTED_CANDIDATE_COUNT = {
        "16 Dufour's Place": 1,
        "City Tower": 1,
        "166 Piccadilly": 1,
        "Kent House": 1,
        "Elm Yard": 1,
        "170 Piccadilly": 2,
        "Thirty One Alfred Place": 2,
        "Nineteen Wells Street": 2,
        "Elsley": 2,
    }

    candidates_by_building = {}
    mismatched = []
    missing = []
    for r in records:
        building = r.get("Building")
        candidates = r.get("_high_res_candidates") or []
        if not candidates:
            missing.append(building)
            continue
        if building in candidates_by_building and candidates_by_building[building] != tuple(candidates):
            mismatched.append(building)
        candidates_by_building[building] = tuple(candidates)

    if missing:
        failures.append(
            f"{filename}: expected every row to have at least one real High Res Images candidate, but these had "
            f"none: {missing} — GPE's building-photo extraction may be broken again"
        )
    if mismatched:
        failures.append(
            f"{filename}: these buildings had inconsistent High Res Images candidates across their own floor rows: "
            f"{mismatched} — a listing's own multiple floors should all share the same building photo(s)"
        )

    # The check that would have caught the off-by-one bug: every distinct
    # building must map to a *distinct* candidate set (no two different
    # buildings sharing a photo) - a naive count-based check can't detect
    # a photo silently attributed to the wrong (adjacent) building.
    all_photos = [url for urls in candidates_by_building.values() for url in urls]
    distinct_photos = set(all_photos)
    if len(all_photos) != len(distinct_photos):
        failures.append(
            f"{filename}: {len(all_photos)} total candidate photo(s) across all buildings but only "
            f"{len(distinct_photos)} distinct URLs — at least one photo is being shared between two buildings "
            "that should each have their own"
        )

    # The check that would have caught the one-photo-per-building
    # assumption: some buildings genuinely have 2 distinct photos, not 1.
    count_mismatches = []
    for building, expected in EXPECTED_CANDIDATE_COUNT.items():
        actual = len(candidates_by_building.get(building, ()))
        if actual != expected:
            count_mismatches.append(f"{building}: expected {expected}, got {actual}")
    if count_mismatches:
        failures.append(
            f"{filename}: unexpected High Res Images candidate count(s) for these buildings: {count_mismatches}"
        )

    if (
        not missing
        and not mismatched
        and len(all_photos) == len(distinct_photos)
        and not count_mismatches
        and floor_plan_count == 0
    ):
        print(
            f"OK  {filename}: High Res Images candidates correctly populated and distinct for all "
            f"{len(candidates_by_building)} buildings ({len(records)} rows, {len(distinct_photos)} distinct photos), "
            "Floor Plan correctly blank"
        )


def check_knotel_records(failures):
    """Targeted regression test for extraction.rules.knotel, pinning known-
    correct Building text — including the real postcode — directly from
    the raw source text of both example emails. Not just "is a postcode
    present" but "is it the RIGHT one": a real, shipped regression had
    every Knotel row's Building silently reduced to just the bare
    marketing name (e.g. "Hallmark", "Gilray House") with the source's own
    real full-street-address-plus-postcode line discarded entirely, even
    though the earlier "min record count" check in EXPECTATIONS above kept
    passing the whole time (right count, wrong content) — confirmed
    against a real live email where the source genuinely has a full
    address for every single listing, so this rule should never need to
    fall back to bare-name geocoding at all.

    Also guards against a second, sharper bug from the same root cause:
    the old "is this a fresh building" gate required the address line to
    contain a full two-part postcode, which silently failed for a
    building whose address only carries a partial/outward-only postcode
    ("Market Exchange" at "8 Macklin Street, Covent Garden WC2", no inward
    code) — current_building then never updated away from the previous
    building ("33 Soho"), so that floor was attributed to entirely the
    wrong building. A naive count or "Building is non-empty" check can't
    catch this (both buildings' rows were still "populated", just with
    one of them wrong) — checking by Floor/Unit (a stable per-row
    identifier the source itself provides) against the expected Building
    catches it directly, the same way check_gpe_high_res_images above
    catches a photo misattributed to the *next* building instead of a
    missing one.

    Also pins three more real fields, each with its own real-world catch:
    Contacts (Knotel gives no individual broker name — only a shared team
    contact in its intro paragraph — must resolve to the real email/phone
    there, not the recipient's own forwarding signature or the forwarded
    email's From:/To: header addresses, both of which sit right next to
    it in the raw text); Special Features (a real per-floor price-drop
    note in the 13/07 email must land on exactly the two 15 Hatfields
    rows it names and nowhere else — checked against both fixtures so a
    regression that leaked it onto an unrelated row, or dropped it
    entirely, both fail); and Brochure PDF (pins two real, confirmed
    source-HTML quirks that would otherwise silently drop a genuine
    brochure link — "View brochure" with inconsistent casing for 33 Soho,
    and text/href entirely reversed for 23 Great Titchfield Street — plus
    Rufus House, which has no brochure button in the source at all and
    must stay blank rather than picking up a neighboring listing's link)."""
    # Pinned by list index — parse() order is deterministic for a fixed,
    # checked-in source file, and several of these buildings share the
    # same Area and Floor/Unit label as each other (e.g. three different
    # City Fringe buildings each have a plain "2nd Floor" listing), so
    # index position is a more reliable identifier here than either field
    # alone. Only the indices relevant to a reported regression are
    # pinned, not all 16/15 rows.
    KNOTEL_CONTACTS = "Knotel Brokers, londonbrokers@knotel.com, 0204 571 4271"
    checks = [
        (
            "Fw_ Knotel Availability _ 30_06_2026.eml",
            16,
            {
                2: dict(
                    building="The Hallmark Building, 106 Fenchurch St, London EC3M 5JE",
                    floor="Hallmark 6th Floor",
                    postcode="EC3M 5JE",
                    brochure="https://pitch.com/v/hallmark-6th-floor-jdfuuc",
                ),
                4: dict(
                    building="Classic House, 174-180 Martha's Buildings, Old St, London EC1V 9BP",
                    floor="2nd Floor",
                    postcode="EC1V 9BP",
                    brochure="https://pitch.com/v/classic-house-8ft3xk",
                ),
                5: dict(
                    building="Gilray House, 146-150 City Rd, London EC1V 2RL",
                    floor="3rd Floor",
                    postcode="EC1V 2RL",
                    brochure="https://pitch.com/v/gilray-house-qg4d3k",
                ),
                6: dict(
                    building="Gilray House, 146-150 City Rd, London EC1V 2RL",
                    floor="4th Floor",
                    postcode="EC1V 2RL",
                    brochure="https://pitch.com/v/gilray-house-qg4d3k",
                ),
                7: dict(
                    building="Rufus House, 2-4 Rufus St, London N1 6PE",
                    floor="2nd Floor",
                    postcode="N1 6PE",
                    # No "View Brochure" button for this listing at all in
                    # the real source — blank is the honest, correct value.
                    brochure="",
                ),
                9: dict(
                    building="15 Hatfields, Chadwick Court, London SE1 8DJ",
                    floor="15 Hatfields - 1st Floor",
                    postcode="SE1 8DJ",
                    brochure="https://pitch.com/v/15-hatfield-wajq9e",
                    # No price-drop promo in this older email at all.
                    special_features="",
                ),
                10: dict(
                    building="15 Hatfields, Chadwick Court, London SE1 8DJ",
                    floor="15 Hatfields - 3rd Floor",
                    postcode="SE1 8DJ",
                    brochure="https://pitch.com/v/15-hatfield-wajq9e",
                    special_features="",
                ),
                11: dict(
                    building="7 Howick Place, 7 Howick Pl, London SW1P 1BB",
                    floor="3rd Floor",
                    postcode="SW1P 1BB",
                    brochure="https://app.pitch.com/app/presentation/3761848b-50da-445a-9e78-49e665889bfb/6572cd6a-3cbe-414b-8d46-a16f9cf6d02a",
                ),
                12: dict(
                    building="23 Great Titchfield Street, 23 Great Titchfield St London W1W 7JA",
                    floor="3B",
                    postcode="W1W 7JA",
                    # Real source HTML has this one anchor's text/href
                    # entirely reversed (href literally = "View Brochure",
                    # visible text = the real URL) — this pins the recovered
                    # real link, not the swapped placeholder.
                    brochure="https://pitch.com/v/23-great-titchfield-street-c6ahp2",
                ),
                14: dict(
                    building="Market Exchange, 8 Macklin Street, Covent Garden WC2",
                    floor="2nd - East Wing",
                    postcode="",
                    brochure="https://pitch.com/v/market-exchange-brochure-8quwwk",
                ),
            },
        ),
        (
            "Fw_ Knotel Availability _ 13_07_2026.eml",
            15,
            {
                9: dict(
                    building="15 Hatfields, Chadwick Court, London SE1 8DJ",
                    floor="15 Hatfields - 1st Floor",
                    postcode="SE1 8DJ",
                    brochure="https://pitch.com/v/15-hatfield-wajq9e",
                    # The real promo note for this exact row/price — must
                    # match the "1st Floor" price, not the "3rd Floor" one.
                    special_features="Price drop: now £120 psf",
                ),
                10: dict(
                    building="15 Hatfields, Chadwick Court, London SE1 8DJ",
                    floor="15 Hatfields - 3rd Floor",
                    postcode="SE1 8DJ",
                    brochure="https://pitch.com/v/15-hatfield-wajq9e",
                    special_features="Price drop: now £115 psf",
                ),
                # The exact regression case: two adjacent West End listings
                # with genuinely different buildings — "33 Soho" must not
                # leak onto the "Market Exchange" row that follows it. Also
                # covers the "View brochure" (lowercase b) casing quirk.
                13: dict(
                    building="33 soho square, W1D 3QU",
                    floor="2nd Floor",
                    postcode="W1D 3QU",
                    brochure="https://pitch.com/v/33-soho-square-w1d-7i96p7",
                ),
                14: dict(
                    building="Market Exchange, 8 Macklin Street, Covent Garden WC2",
                    floor="2nd - East Wing",
                    # This building's own address only ever gives a partial,
                    # outward-only postcode ("WC2", no inward part) — "" is
                    # the honest, correct extraction here, not a bug.
                    postcode="",
                    brochure="https://pitch.com/v/market-exchange-brochure-8quwwk",
                ),
            },
        ),
    ]

    for filename, expected_count, expected_by_index in checks:
        path = ROOT / filename
        if not path.exists():
            failures.append(f"{filename}: example file not found (expected at {path})")
            continue

        content = read_file(path)
        rule_name, records = try_rules(content)
        if rule_name != "Knotel" or not records:
            failures.append(f"{filename}: expected rule 'Knotel' with records, got '{rule_name}'")
            continue
        if len(records) != expected_count:
            failures.append(f"{filename}: expected {expected_count} records, got {len(records)}")

        normalized = [normalize_record(r) for r in records]
        local_failures = []
        for idx, expected in expected_by_index.items():
            if idx >= len(normalized):
                local_failures.append(f"{filename}: expected a record at index {idx}, only {len(normalized)} present")
                continue
            row = normalized[idx]
            expected_floor = expected["floor"]
            if row["Floor/Unit"] != expected_floor:
                local_failures.append(
                    f"{filename}: record {idx} expected Floor/Unit {expected_floor!r}, got {row['Floor/Unit']!r} "
                    "(fixture may have changed, or record ordering shifted)"
                )
            if row["Building"] != expected["building"]:
                local_failures.append(
                    f"{filename}: record {idx} ({expected_floor}) expected Building {expected['building']!r}, got {row['Building']!r}"
                )
            if row["Property Postcode"] != expected["postcode"]:
                local_failures.append(
                    f"{filename}: record {idx} ({expected_floor}) expected Property Postcode {expected['postcode']!r}, "
                    f"got {row['Property Postcode']!r}"
                )
            if row["Brochure PDF"] != expected["brochure"]:
                local_failures.append(
                    f"{filename}: record {idx} ({expected_floor}) expected Brochure PDF {expected['brochure']!r}, "
                    f"got {row['Brochure PDF']!r}"
                )
            expected_special_features = expected.get("special_features")
            if expected_special_features is not None and row["Special Features"] != expected_special_features:
                local_failures.append(
                    f"{filename}: record {idx} ({expected_floor}) expected Special Features {expected_special_features!r}, "
                    f"got {row['Special Features']!r}"
                )

        # Every row (regardless of index) shares the same whole-email
        # contact — Knotel gives no individual broker name, just a shared
        # team email/phone in the intro paragraph.
        contacts_values = {r["Contacts"] for r in normalized}
        if contacts_values != {KNOTEL_CONTACTS}:
            local_failures.append(f"{filename}: expected Contacts == {KNOTEL_CONTACTS!r} for every row, got {contacts_values}")

        # Assigned Agents is a NAME-ONLY subset of Contacts, not a
        # duplicate of it — must drop the email/phone that Contacts
        # itself correctly keeps.
        assigned_agents_values = {r["Assigned Agents"] for r in normalized}
        if assigned_agents_values != {"Knotel Brokers"}:
            local_failures.append(
                f"{filename}: expected Assigned Agents == 'Knotel Brokers' (name only, no email/phone) for every row, "
                f"got {assigned_agents_values}"
            )

        # Every OTHER row (not one of the two 15 Hatfields price-drop rows
        # above) must have blank Special Features — guards against the
        # promo note leaking onto an unrelated building/floor.
        unexpected_special_features = {
            (i, r["Building"], r["Floor/Unit"]): r["Special Features"]
            for i, r in enumerate(normalized)
            if r["Special Features"] and expected_by_index.get(i, {}).get("special_features") != r["Special Features"]
        }
        if unexpected_special_features:
            local_failures.append(f"{filename}: unexpected Special Features on rows not covered by the price-drop promo: {unexpected_special_features}")

        # Knotel is a lettings-only source — it never sets a Sale Price
        # signal (see extraction.schema.normalize_record), so For Sale
        # must stay "No" for every row even when the email's own
        # promotional copy mentions a price drop (confirmed against the
        # real "** PRICE DROP AT 15 HATFIELDS **" banner in both example
        # emails) — a promotional/discount mention is not a sale signal.
        for_sale_values = {r["For Sale"] for r in normalized}
        if for_sale_values != {"No"}:
            local_failures.append(
                f"{filename}: expected For Sale == 'No' for every row (lettings-only source), got {for_sale_values}"
            )

        if not local_failures:
            print(f"OK  {filename}: {len(records)} Knotel records spot-checked against known-correct source values")
        failures.extend(local_failures)


def check_street_address_only(failures):
    """Targeted regression test for extraction.schema.street_address_only —
    pins real Building values pulled directly from every example source
    (not hand-invented strings) against the exact expected "Building
    Name, Street Number Street Name" result (name kept when one genuinely
    exists, postcode never kept), plus the illustrative Crown Estate-style
    "Princes House, 38 Jermyn Street, SW1Y" case from extraction.pipeline's
    own docstring. Deliberately a pure function-level check (no file I/O
    needed for most cases) since street_address_only never touches
    geocoding — see its own docstring for why it's called from
    extraction.pipeline.process_files only AFTER geocoding has already
    run, not from schema.normalize_record."""
    cases = [
        # Knotel — the "Name, Street, London POSTCODE" shape (both name
        # and street real, no overlap between them) — name is kept.
        ("Gilray House, 146-150 City Rd, London EC1V 2RL", "Gilray House, 146-150 City Rd"),
        ("The Hallmark Building, 106 Fenchurch St, London EC3M 5JE", "The Hallmark Building, 106 Fenchurch St"),
        ("Rufus House, 2-4 Rufus St, London N1 6PE", "Rufus House, 2-4 Rufus St"),
        # Knotel — "Street, POSTCODE" shape (no separate name at all) —
        # nothing to combine, just the street.
        ("2 Leonard Circus, EC2A 4LW", "2 Leonard Circus"),
        # Knotel — 3-comma-segment shape where the last segment (the real
        # street) has no digit of its own ("Old St") but an earlier one
        # does ("174-180 Martha's Buildings") — the digit-bearing segment
        # is kept, "Old St" is dropped, and the name before it ("Classic
        # House") IS kept since it precedes the digit-bearing segment.
        (
            "Classic House, 174-180 Martha's Buildings, Old St, London EC1V 9BP",
            "Classic House, 174-180 Martha's Buildings",
        ),
        # Knotel — no comma at all before "London POSTCODE".
        (
            "23 Great Titchfield Street, 23 Great Titchfield St London W1W 7JA",
            "23 Great Titchfield Street, 23 Great Titchfield St",
        ),
        # Knotel — the real regression case that motivated the digit-
        # preference rule in the first place: the marketing name ("6
        # Maiden Lane") IS the real street+number, while the address line
        # itself only ever gives a neighbourhood name with no number
        # ("Covent Garden") — the digit-bearing segment IS the first one
        # here, so there's nothing before it to keep as a separate name;
        # must not end up with "Covent Garden" (a neighbourhood, not a
        # street) attached at all.
        ("6 Maiden Lane, Covent Garden, WC2E 7ND", "6 Maiden Lane"),
        # Knotel — a neighbourhood name combined with only a PARTIAL
        # (outward-only) postcode in the very same segment, no comma
        # between them at all ("Covent Garden WC2") — name kept, the
        # neighbourhood+partial-postcode segment dropped entirely.
        ("Market Exchange, 8 Macklin Street, Covent Garden WC2", "Market Exchange, 8 Macklin Street"),
        # Breezblok — same "Name, Street, London POSTCODE" shape as Knotel.
        ("John Stow House, 18 Bevis Marks, London EC3A 7JB", "John Stow House, 18 Bevis Marks"),
        # Knotel — the digit-bearing segment is the FIRST one (a
        # marketing name that's itself a real street+number), with a
        # later non-numbered alternate name ("Chadwick Court") that's
        # correctly dropped rather than force-combined.
        ("15 Hatfields, Chadwick Court, London SE1 8DJ", "15 Hatfields"),
        # GPE/MetSpace/BC — Building is already just a street name/number
        # with no postcode or separate marketing name at all (nothing to
        # strip) — must pass through completely unchanged.
        ("9-10 Market Place", "9-10 Market Place"),
        ("170 Piccadilly", "170 Piccadilly"),
        ("10-12 Alie Street", "10-12 Alie Street"),
        # GPE/BC — a bare marketing name with no separate address info in
        # the source at all (no digit, no postcode) — the "should be
        # rare" case where nothing better is available; must not be
        # blanked out, just passed through as the best available text.
        ("Elsley", "Elsley"),
        ("Porters Place", "Porters Place"),
        # Kitt's — "Name, Street" shape with no postcode at all — name kept.
        ("The Hide, 3 Kingly Court", "The Hide, 3 Kingly Court"),
        ("Bridge House, 22 Newman Street", "Bridge House, 22 Newman Street"),
        # Illustrative Crown Estate/LLM-fallback-style example from
        # extraction.pipeline's own module docstring.
        ("Princes House, 38 Jermyn Street, SW1Y", "Princes House, 38 Jermyn Street"),
    ]
    local_failures = [
        f"street_address_only({building!r}) expected {expected!r}, got {street_address_only(building)!r}"
        for building, expected in cases
        if street_address_only(building) != expected
    ]
    if not local_failures:
        print(f"OK  street_address_only: {len(cases)} real Building values spot-checked against known-correct results")
    failures.extend(local_failures)


def check_names_only(failures):
    """Targeted regression test for extraction.schema.names_only, which
    extraction.schema.normalize_record uses to derive Assigned Agents
    from Contacts — must strip an email/phone number out of a combined
    contact string (Knotel's real "Knotel Brokers, londonbrokers@
    knotel.com, 0204 571 4271" -> "Knotel Brokers") while leaving a
    Contacts value that's already just name(s) completely unchanged
    (Kitt's real "Leah Noray, Ben Danaher", Breezblok's real "Sales")."""
    cases = [
        ("Knotel Brokers, londonbrokers@knotel.com, 0204 571 4271", "Knotel Brokers"),
        ("Leah Noray, Ben Danaher", "Leah Noray, Ben Danaher"),
        ("Kieran Christie, Sophie Haugh, Nicki Mayle", "Kieran Christie, Sophie Haugh, Nicki Mayle"),
        ("Sales", "Sales"),
        ("", ""),
    ]
    local_failures = [
        f"names_only({contacts!r}) expected {expected!r}, got {names_only(contacts)!r}"
        for contacts, expected in cases
        if names_only(contacts) != expected
    ]
    if not local_failures:
        print(f"OK  names_only: {len(cases)} real Contacts values spot-checked against known-correct results")
    failures.extend(local_failures)


def check_geocode_same_building_ambiguity(failures):
    """Targeted regression test for extraction.geocode._check_ambiguity's
    SAME_BUILDING_DISTANCE_KM check — a 2026-07 MetSpace audit found real,
    independently cross-checked wrong postcodes for two buildings ("9-10
    Market Place" showed W1W 8AE, really W1W 8AQ per Savills/CBRE/
    Workthere/Hubble/Rightmove; "1 Curtain Road" showed EC2A 3NY, really
    EC2A 3JX per a direct commercial listing) caused by a single Nominatim
    query returning two genuinely different OSM nodes — one per unit/
    entrance of the SAME building (an office node and a restaurant/bar
    node) — just a few metres apart with different postcodes, which the
    existing far-apart AMBIGUITY_DISTANCE_KM check (built for a
    completely different failure mode — Nominatim returning a confident
    match many km away in an unrelated borough) can't catch at all.
    Pure-function unit test (no live network needed) against the real
    coordinates/postcodes Nominatim actually returned for both cases,
    plus a close-same-postcode case (must NOT trip) and a far-apart case
    (must still trip the original, unrelated check)."""
    cases = [
        # Real case: "9-10 Market Place" — office node vs. restaurant node
        # ~1m apart, different postcodes. Must be flagged ambiguous.
        (51.5164430, -0.1402766, "W1W 8AE", [(51.5164451, -0.1402630, "W1W 8AQ")], True),
        # Real case: "1 Curtain Road" — office node vs. bar node ~5m apart,
        # different postcodes. Must be flagged ambiguous.
        (51.5221940, -0.0811526, "EC2A 3NY", [(51.5221450, -0.0811478, "EC2A 3JX")], True),
        # Close candidates that agree on postcode — same building, no real
        # conflict — must NOT be flagged.
        (51.5164430, -0.1402766, "W1W 8AE", [(51.5164451, -0.1402630, "W1W 8AE")], False),
        # Far apart (the original, unrelated failure mode) — must still trip.
        (51.5127184, -0.1412715, "W1S 2YZ", [(51.4, -0.3, "N9 0AA")], True),
        # Close candidates where the second has no postcode at all —
        # nothing to disagree with, must NOT be flagged.
        (51.5164430, -0.1402766, "W1W 8AE", [(51.5164451, -0.1402630, "")], False),
    ]
    local_failures = []
    for lat, lng, postcode, other_candidates, expect_ambiguous in cases:
        error = geocode_module._check_ambiguity(lat, lng, postcode, other_candidates)
        is_ambiguous = error is not None
        if is_ambiguous != expect_ambiguous:
            local_failures.append(
                f"_check_ambiguity({lat}, {lng}, {postcode!r}, {other_candidates}) expected "
                f"ambiguous={expect_ambiguous}, got ambiguous={is_ambiguous} (error={error!r})"
            )
    if not local_failures:
        print(f"OK  geocode same-building ambiguity: {len(cases)} cases (2 real, cross-checked) spot-checked")
    failures.extend(local_failures)


def check_contacts_include_phone_email(failures):
    """Targeted regression test for a class of gap a 2026-07 audit found
    across MetSpace, GPE, and Breezblok, prompted by Knotel's own earlier
    "Contacts was entirely blank" bug: each of these sources' contact
    block DOES have a real phone and/or email for every named contact,
    genuinely present in the source, that was being silently dropped —
    only the name(s) ever made it into Contacts. Pins the exact,
    real Contacts value (name + phone/email) for each, and confirms
    Assigned Agents (extraction.schema.names_only) still correctly
    reduces it back to just the name(s) — including GPE's own
    international "+44 (0) ..." phone format, which an earlier version of
    names_only's own phone-detection regex (UK-domestic-only) failed to
    recognize, silently leaking the phone number into Assigned Agents
    instead of stripping it."""
    cases = [
        (
            "Fw_ MetSpace Availability Update.eml",
            "MetSpace",
            "Kieran Christie, sales@metspace.co.uk, 07837 270 455, Sophie Haugh, sales@metspace.co.uk, "
            "07950 565 491, Nicki Mayle, sales@metspace.co.uk, 07946 136 004",
            "Kieran Christie, Sophie Haugh, Nicki Mayle",
        ),
        (
            "Fw_ The latest GPE Fully Managed availability – workspaces you won't want to miss..eml",
            "GPE",
            "David Korman, +44 (0) 7435 939 956, Molly Maguire, +44 (0) 7887 841 816, Anna Tweed, "
            "+44 (0) 7990 633 486, Richard Carson, +44 (0) 7436 030 120",
            "David Korman, Molly Maguire, Anna Tweed, Richard Carson",
        ),
        (
            "John Stow House.pdf",
            "Breezblok",
            "Sales, Sales@breezblok.london, +44 7500665267",
            "Sales",
        ),
    ]
    for filename, expected_rule, expected_contacts, expected_agents in cases:
        path = ROOT / filename
        if not path.exists():
            failures.append(f"{filename}: example file not found (expected at {path})")
            continue
        content = read_file(path)
        rule_name, records = try_rules(content)
        if rule_name != expected_rule or not records:
            failures.append(f"{filename}: expected rule '{expected_rule}' with records, got '{rule_name}'")
            continue
        norm = normalize_record(records[0])
        local_failures = []
        if norm["Contacts"] != expected_contacts:
            local_failures.append(f"{filename}: expected Contacts {expected_contacts!r}, got {norm['Contacts']!r}")
        if norm["Assigned Agents"] != expected_agents:
            local_failures.append(f"{filename}: expected Assigned Agents {expected_agents!r}, got {norm['Assigned Agents']!r}")
        if not local_failures:
            print(f"OK  {filename}: Contacts/Assigned Agents spot-checked against known-correct source values")
        failures.extend(local_failures)


def check_bc_records(failures):
    """Targeted regression test for extraction.rules.bc, pinning known-correct
    field values from the real "BC Current Availability.pdf" table.

    Also guards against a real bug this rule already had: an earlier,
    looser version of its detect() (generic single-word keywords, the same
    style as extraction.rules.grid) matched Kitt's own table too, since
    Kitt's header already uses near-schema wording ("Building", "Floor/
    Unit", "Size (sq ft)", "Desks (max)", "Marketing Price...PCM") that a
    loose keyword count can't tell apart from BC's own generic English
    column names. Fixed by requiring BC's own distinctive "Num of Desks" +
    "Sale Price" combination, which Kitt's table doesn't have — this test
    would catch a future regression back to that looser detection by
    failing the "got 'BC'" assertion in the main EXPECTATIONS loop above
    for Kitt's own file, but pins the BC-specific values here too so a
    change to bc.py's column mapping doesn't silently misparse a value."""
    filename = "BC Current Availability.pdf"
    path = ROOT / filename
    if not path.exists():
        failures.append(f"{filename}: example file not found (expected at {path})")
        return

    content = read_file(path)
    rule_name, records = try_rules(content)
    if rule_name != "BC" or not records:
        failures.append(f"{filename}: expected rule 'BC' with records, got '{rule_name}'")
        return

    normalized = [normalize_record(r) for r in records]
    by_key = {(r["Building"], r["Floor/Unit"]): r for r in normalized}

    # A handful of known rows, re-verified directly against the source
    # table — covers a row with a real Sale Price (For Sale should be
    # "Yes"), a row with "N/A" (For Sale should be "No"), and the PCM/PSF
    # derivation (this source only gives PCM; PSF must be computed).
    checks = [
        (
            ("10-12 Alie Street", "G & LG Duplex"),
            {
                "Size (sq ft)": 4800,
                "Desks (max)": 70,
                "Marketing Price (Based on Min Term) PCM": 48000,
                "Marketing Price (Based on Min Term) PSF": 120.0,
                "Special Features": "Communal Lounge, Break-out & Terrace",
                "For Sale": "Yes",
                # BC's own table has no contact/agent column at all (see
                # this module's docstring) — Assigned Agents must fall
                # back to "Unknown", not be left blank or invent one.
                "Assigned Agents": "Unknown",
            },
        ),
        (
            ("17 Bevis Marks", "3rd Floor"),
            {
                "Size (sq ft)": 3200,
                "Desks (max)": 50,
                "Marketing Price (Based on Min Term) PCM": 35000,
                "For Sale": "No",
                "Assigned Agents": "Unknown",
            },
        ),
        (
            ("Porters Place", "4th Floor"),
            {
                "Size (sq ft)": 3476,
                "Desks (max)": 50,
                "Marketing Price (Based on Min Term) PCM": 55036,
                "State of Space": "Immediate",
                "For Sale": "No",
                "Assigned Agents": "Unknown",
            },
        ),
    ]
    local_failures = []
    for key, expected in checks:
        row = by_key.get(key)
        if not row:
            local_failures.append(f"{filename}: expected a row for {key}, not found")
            continue
        for field, expected_value in expected.items():
            if row.get(field) != expected_value:
                local_failures.append(
                    f"{filename}: {key} field {field!r} expected {expected_value!r}, got {row.get(field)!r}"
                )

    if len(records) != 11:
        local_failures.append(f"{filename}: expected 11 records, got {len(records)}")

    if not local_failures:
        print(f"OK  {filename}: {len(records)} BC records spot-checked against known-correct source values")
    failures.extend(local_failures)


def check_breezblok_records(failures):
    """Targeted regression test for extraction.rules.breezblok, pinning
    known-correct field values from the real "John Stow House.pdf"
    brochure — a single-listing, multi-page format where the building's
    own address/postcode and the listing's own size/desks/price sit on
    different pages, not one table."""
    filename = "John Stow House.pdf"
    path = ROOT / filename
    if not path.exists():
        failures.append(f"{filename}: example file not found (expected at {path})")
        return

    content = read_file(path)
    rule_name, records = try_rules(content)
    if rule_name != "Breezblok" or not records:
        failures.append(f"{filename}: expected rule 'Breezblok' with records, got '{rule_name}'")
        return
    if len(records) != 1:
        failures.append(f"{filename}: expected exactly 1 record (one 'Proposed space' section), got {len(records)}")
        return

    norm = normalize_record(records[0])
    expected = {
        "Building": "John Stow House, 18 Bevis Marks, London EC3A 7JB",
        "Floor/Unit": "Office 302",
        "Size (sq ft)": 1750,
        "Desks (max)": 32,
        "Marketing Price (Based on Min Term) PCM": 18000,
        # The real email/phone from the two lines following "Contact:
        # Sales" in the source — found missing in a 2026-07 audit (the
        # same class of gap as Knotel's own originally-missed contact
        # info) and fixed to be captured alongside the name.
        "Contacts": "Sales, Sales@breezblok.london, +44 7500665267",
        # Not an individual's name, but Breezblok's own source names no
        # one else either — see extraction.rules.breezblok._contact's own
        # docstring for why this is the correct, non-blank value.
        # names_only() must strip the email/phone above but leave "Sales"
        # itself unchanged.
        "Assigned Agents": "Sales",
        "Property Postcode": "EC3A 7JB",
    }
    local_failures = [
        f"{filename}: field {field!r} expected {expected_value!r}, got {norm.get(field)!r}"
        for field, expected_value in expected.items()
        if norm.get(field) != expected_value
    ]

    # Property Address 1 itself isn't set to the clean value by
    # normalize_record (see street_address_only's own docstring for why
    # that's deliberately deferred to extraction.pipeline.process_files,
    # after geocoding) — check the derivation function directly instead.
    # Keeps the building name ("John Stow House") alongside the street,
    # not just the street alone — Building itself is untouched either way.
    derived_street = street_address_only(norm["Building"])
    if derived_street != "John Stow House, 18 Bevis Marks":
        local_failures.append(
            f"{filename}: street_address_only(Building) expected 'John Stow House, 18 Bevis Marks', got {derived_street!r}"
        )

    if not local_failures:
        print(f"OK  {filename}: Breezblok record spot-checked against known-correct source values")
    failures.extend(local_failures)


def check_pdf_floorplan_vs_photos(failures, filename, building, name, expect_gallery):
    """Targeted regression test for two real bugs found in the same
    classification step, across two different PDF sources:

    1. BC's "2-7 Clerkenwell Green" brochure: Floor Plan showed the text
    "Example Floorplan" with no hyperlink attached at all — the LLM
    fallback was copying that literal heading into the Floor Plan field
    itself (a plain string, never a real link), and nothing overwrote it
    since app.py's own image-based Floor Plan logic didn't exist yet at
    the time. Fixed in two places: extraction.llm_fallback no longer asks
    the LLM for (or trusts it for) Floor Plan/High Res Images at all, and
    app.py's _attach_pdf_images now sets a real Floor Plan link itself
    when it finds one.

    2. Breezblok's "John Stow House" brochure: Floor Plan was blank
    despite page 6 genuinely containing a floor-plan diagram, while High
    Res Images correctly showed a gallery — the diagram had been silently
    swept into the photo gallery instead of recognized as a floor plan.
    Root cause: classification worked at PAGE granularity (a whole page
    was "the floor plan page" or not, based on that page's own text
    mentioning "floor plan") and this page's text never says so — yet the
    same page also has a genuine desk photo alongside the diagram. Fixed
    by classifying each image individually (extraction.pdf_images.
    is_floorplan_image, a pixel-content signal: a floor-plan diagram is
    rendered on a plain white background, confirmed empirically far
    whiter than any real photo or decorative logo graphic tested) instead
    of by page.

    This calls the real app.py code path (_attach_pdf_images) directly
    rather than reimplementing its logic, and checks actual byte-identity
    against the source PDF's own images — not just "is something
    populated" — so a future regression that mixes up which image is
    which would be caught, not just a blank-vs-populated regression."""
    path = ROOT / filename
    if not path.exists():
        failures.append(f"{filename}: example file not found (expected at {path})")
        return

    content = read_file(path)
    pages_text = content.get("pages_text", [])
    page_images = pdf_images.extract_page_images(path)
    local_failures = []

    # Independently determine ground truth: which real, extracted image(s)
    # this pixel/text classification calls a floor plan vs a photo, so the
    # assertions below check against the same source data app.py itself
    # would see, not a hand-picked expectation that could drift.
    floorplan_hashes = set()
    photo_hashes = set()
    for page_num, imgs in page_images.items():
        page_is_labeled_floorplan = pdf_images.is_floorplan_page(pages_text[page_num] if page_num < len(pages_text) else "")
        for image_bytes, _ext, _link_floorplan_url in imgs:
            h = hashlib.sha256(image_bytes).hexdigest()
            if page_is_labeled_floorplan or pdf_images.is_floorplan_image(image_bytes):
                floorplan_hashes.add(h)
            else:
                photo_hashes.add(h)

    if not floorplan_hashes:
        failures.append(
            f"{filename}: expected at least one image to be classified as a floor plan (this source is known to "
            "contain a real floor-plan diagram) — floor-plan classification may be broken"
        )
        return
    if not photo_hashes:
        failures.append(f"{filename}: expected at least one real (non-floor-plan) photo — got none")
        return

    records = [{"Building": building}]
    with tempfile.TemporaryDirectory() as tmp:
        batch_dir = Path(tmp)
        with app_module.app.test_request_context():
            jobs = app_module._attach_pdf_images(records, path, pages_text, batch_dir, "testbatch", name)

        saved_paths = {key: local_path for key, local_path in jobs}
        record = records[0]
        floor_plan_url = record.get("Floor Plan", "")
        high_res_url = record.get("High Res Images", "")

        if not floor_plan_url:
            local_failures.append(f"{filename}: expected Floor Plan populated with a real link, got blank")
        if not high_res_url:
            local_failures.append(f"{filename}: expected High Res Images populated with a real link, got blank")
        if floor_plan_url and high_res_url:
            if floor_plan_url == high_res_url:
                local_failures.append(f"{filename}: Floor Plan and High Res Images point to the same URL")
            else:
                # Resolve whichever local file(s) actually ended up
                # referenced by each column, and hash them back against
                # the ground truth above.
                def _referenced_hashes(url):
                    if url.rstrip("/").split("?", 1)[0].endswith(".html"):
                        gallery_path = next((p for k, p in saved_paths.items() if k.split("/")[-1] in url), None)
                        if gallery_path is None:
                            return set()
                        html = Path(gallery_path).read_text(encoding="utf-8")
                        return {
                            hashlib.sha256(Path(p).read_bytes()).hexdigest()
                            for k, p in saved_paths.items()
                            if k.split("/")[-1] in html
                        }
                    local_path = next((p for k, p in saved_paths.items() if k.split("/")[-1] in url), None)
                    return {hashlib.sha256(Path(local_path).read_bytes()).hexdigest()} if local_path else set()

                floor_plan_hashes_used = _referenced_hashes(floor_plan_url)
                high_res_hashes_used = _referenced_hashes(high_res_url)

                if not floor_plan_hashes_used & floorplan_hashes:
                    local_failures.append(f"{filename}: Floor Plan link doesn't point to a real floor-plan-classified image")
                if floor_plan_hashes_used & photo_hashes:
                    local_failures.append(
                        f"{filename}: Floor Plan link points to what was classified as a real photo, not the floor plan"
                    )
                if high_res_hashes_used & floorplan_hashes:
                    local_failures.append(
                        f"{filename}: the floor-plan image was found inside High Res Images (gallery or direct link) — "
                        "it should be excluded from the photo gallery and only appear in Floor Plan"
                    )
                if not (high_res_hashes_used & photo_hashes):
                    local_failures.append(f"{filename}: High Res Images doesn't reference any real photo")
                if expect_gallery and "gallery" not in high_res_url:
                    local_failures.append(
                        f"{filename}: expected High Res Images to be a gallery page (2+ real photos), got a direct link"
                    )

                if not local_failures:
                    print(
                        f"OK  {filename}: Floor Plan correctly linked to the real floor-plan image, kept separate "
                        f"from High Res Images ({len(high_res_hashes_used)} real photo(s) referenced, floor plan "
                        "excluded)"
                    )

    failures.extend(local_failures)


def main():
    failures = []
    for filename, expected_rule, expected_count in EXPECTATIONS:
        path = ROOT / filename
        if not path.exists():
            failures.append(f"{filename}: example file not found (expected at {path})")
            continue

        content = read_file(path)
        rule_name, records = try_rules(content)

        if rule_name != expected_rule:
            failures.append(f"{filename}: expected rule '{expected_rule}', got '{rule_name}'")
            continue

        # Exact, not just a minimum — a "some rows silently dropped"
        # regression (the exact Kitt's multi-table bug this pins) can
        # still clear a loose minimum. A source that's supposed to grow
        # over time should get its own pinned number bumped here rather
        # than switched back to a loose bound.
        if not records or len(records) != expected_count:
            failures.append(f"{filename}: expected exactly {expected_count} records, got {len(records) if records else 0}")
            continue

        for r in records:
            norm = normalize_record(r)
            if not (norm.get("Building") or norm.get("Area")):
                failures.append(f"{filename}: found a record with no Building or Area")
                break

        print(f"OK  {filename}: {len(records)} records via {rule_name}")

    check_metspace_floor_plans(failures)
    check_gpe_high_res_images(failures)
    check_knotel_records(failures)
    check_street_address_only(failures)
    check_names_only(failures)
    check_geocode_same_building_ambiguity(failures)
    check_contacts_include_phone_email(failures)
    check_bc_records(failures)
    check_breezblok_records(failures)
    check_pdf_floorplan_vs_photos(
        failures,
        filename="2nd Floor - 2-7 Clerkenwell Green Brochure.pdf",
        building="2-7 Clerkenwell Green",
        name="BC",
        expect_gallery=True,
    )
    check_pdf_floorplan_vs_photos(
        failures,
        filename="John Stow House.pdf",
        building="John Stow House",
        name="Breezblok",
        expect_gallery=True,
    )

    if failures:
        print("\nFAILURES:")
        for f in failures:
            print(" -", f)
        sys.exit(1)
    print("\nAll example files extracted successfully.")


if __name__ == "__main__":
    main()
