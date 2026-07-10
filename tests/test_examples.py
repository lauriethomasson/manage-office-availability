"""Regression test against the 3 example files this app was built for.
Run with: python tests/test_examples.py
Asserts on rule *names* and minimum record counts (not exact field values —
those are covered by manual review) so this catches "a parser stopped
matching" or "extraction silently dropped most rows" regressions.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from extraction.file_readers import read_file
from extraction.rules import try_rules
from extraction.schema import normalize_record

EXPECTATIONS = [
    ("Fw_ Knotel Availability _ 30_06_2026.eml", "Knotel", 16),
    ("Fw_ MetSpace Availability Update.eml", "MetSpace", 14),
    ("Fw_ The latest GPE Fully Managed availability – workspaces you won't want to miss..eml", "GPE", 15),
    ("Kitt's Availability (External) - Live Availability.pdf", "Grid/Tabular", 19),
]


def check_metspace_floor_plans(failures):
    """Targeted regression test for a real bug that already shipped once:
    MetSpace's rule never extracted Floor Plan/High Res Images at all,
    despite the source email genuinely containing a per-listing floor
    plan image (confirmed by actually viewing several of them - real
    floor-plan diagrams, not building photos, which is why this checks
    Floor Plan specifically and not High Res Images). Pins the exact,
    known-correct counts for this specific example file rather than a
    vague ">0" check, so a future regression (e.g. someone "fixing" the
    html_items image filter and breaking this again) fails loudly here
    instead of only being caught by manually spot-checking a spreadsheet
    later."""
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

    # Known-correct for this exact example file: 13 of 14 listings have a
    # real floor plan image; the first listing genuinely has none (no
    # image precedes its link in the source HTML at all) - not a bug.
    if floor_plan_count < 13:
        failures.append(
            f"{filename}: expected >= 13 records with a real Floor Plan URL, got {floor_plan_count}/{len(records)} "
            "— MetSpace's floor-plan-image extraction may be broken again"
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

    if floor_plan_count >= 13 and high_res_count == 0:
        print(f"OK  {filename}: Floor Plan populated for {floor_plan_count}/{len(records)} rows, High Res Images correctly blank")


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


def main():
    failures = []
    for filename, expected_rule, min_count in EXPECTATIONS:
        path = ROOT / filename
        if not path.exists():
            failures.append(f"{filename}: example file not found (expected at {path})")
            continue

        content = read_file(path)
        rule_name, records = try_rules(content)

        if rule_name != expected_rule:
            failures.append(f"{filename}: expected rule '{expected_rule}', got '{rule_name}'")
            continue

        if not records or len(records) < min_count:
            failures.append(f"{filename}: expected >= {min_count} records, got {len(records) if records else 0}")
            continue

        for r in records:
            norm = normalize_record(r)
            if not (norm.get("Building") or norm.get("Area")):
                failures.append(f"{filename}: found a record with no Building or Area")
                break

        print(f"OK  {filename}: {len(records)} records via {rule_name}")

    check_metspace_floor_plans(failures)
    check_gpe_high_res_images(failures)

    if failures:
        print("\nFAILURES:")
        for f in failures:
            print(" -", f)
        sys.exit(1)
    print("\nAll example files extracted successfully.")


if __name__ == "__main__":
    main()
