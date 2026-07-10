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

    if failures:
        print("\nFAILURES:")
        for f in failures:
            print(" -", f)
        sys.exit(1)
    print("\nAll example files extracted successfully.")


if __name__ == "__main__":
    main()
