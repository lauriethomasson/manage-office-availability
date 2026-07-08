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

    if failures:
        print("\nFAILURES:")
        for f in failures:
            print(" -", f)
        sys.exit(1)
    print("\nAll example files extracted successfully.")


if __name__ == "__main__":
    main()
