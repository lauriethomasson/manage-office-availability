"""Rule-based parsers, one per known sender/layout.

Each module exposes:
  detect(content) -> bool        cheap check: does this look like our layout?
  parse(content) -> list[dict]   raw records (pre-normalization). Should not
                                  raise for minor per-row issues; skip the row.

The pipeline tries each rule's detect() in order and uses the first match's
parse(). If none match, it falls back to the LLM.
"""
from . import knotel, metspace, gpe, grid

RULES = [
    ("Knotel", knotel),
    ("MetSpace", metspace),
    ("GPE", gpe),
    ("Grid/Tabular", grid),
]


def try_rules(content):
    """Returns (rule_name, records) for the first matching rule, or (None, None)."""
    for name, module in RULES:
        try:
            if module.detect(content):
                records = module.parse(content)
                if records:
                    return name, records
        except Exception:
            # A rule erroring out just means it doesn't apply cleanly —
            # fall through to the next rule / eventually the LLM.
            continue
    return None, None
