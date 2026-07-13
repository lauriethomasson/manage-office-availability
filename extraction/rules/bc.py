"""Rule-based parser for BC's own "Current Availability" summary table — a
single-page table listing every currently available floor/desk-cluster
across all of BC's buildings (Building Name/Floor/Size (Sq ft)/Num of
Desks/Market Price/Sale Price/Available/Includes).

Distinct from BC's OTHER PDF format — a single-listing marketing brochure
per building (e.g. "2nd Floor - 2-7 Clerkenwell Green Brochure.pdf") — that
one has no table at all and still goes through the LLM fallback (with its
Floor Plan/High Res Images filled in separately by app.py's
extraction.pdf_images enrichment); this rule only ever matches the tabular
one.

Confirmed empirically that this table's own header row is NOT row 0 of the
extracted table — pdfplumber returns a leading blank spacer row first (an
artifact of this PDF's own layout) — so, unlike extraction.rules.grid
(which assumes the header is always row 0), this scans every row for the
first one that looks like a real header.

Detection deliberately does NOT use the same loose "N of these generic
keywords" approach as extraction.rules.grid: confirmed empirically that
doing so (an earlier version of this file) matched Kitt's own table too —
Kitt's own header already uses near-schema wording ("Building", "Floor/
Unit", "Size (sq ft)", "Desks (max)", "Marketing Price...PCM"), so generic
single-word keywords like "floor"/"size"/"desk" match both formats, and
"market" as a bare keyword even matches inside Kitt's "Marketing Price".
detect() instead requires BC's own genuinely distinctive column combination
— "Num of Desks" and "Sale Price" together, neither of which appears in
Kitt's or any other known source's header — so it can't fire on a
different, differently-labeled tabular source."""

KEYWORDS = {
    "Building": [["building"]],
    "Floor/Unit": [["floor"]],
    "Size (sq ft)": [["size"]],
    "Desks (max)": [["desk"]],
    # An exact phrase, not ["market", "price"] as two independent words —
    # the latter also matches "Marketing Price...", Kitt's own column name.
    "Marketing Price (Based on Min Term) PCM": [["market price"]],
    "Sale Price": [["sale price"]],
    "State of Space": [["available"]],
    "Special Features": [["includes"]],
}

# The specific combination that identifies BC's table and nothing else
# seen so far — "desk" and "price" alone are too generic on their own
# (see module docstring), but no other known source pairs "num of desks"
# with a distinct "sale price" column.
_REQUIRED_SIGNALS = ["num of desks", "sale price"]


def detect(content):
    return _find_table(content) is not None


def parse(content):
    found = _find_table(content)
    if not found:
        return []
    header_idx, col_map, table = found

    records = []
    for row in table[header_idx + 1 :]:
        if not any((c or "").strip() for c in row):
            continue
        record = {}
        for idx, col_name in col_map.items():
            if idx < len(row):
                record[col_name] = (row[idx] or "").strip()
        if record.get("Building"):
            records.append(record)
    return records


def _find_table(content):
    for table in content.get("tables", []):
        for i, row in enumerate(table):
            lowered_cells = [(c or "").lower() for c in row]
            if not all(any(signal in cell for cell in lowered_cells) for signal in _REQUIRED_SIGNALS):
                continue
            return i, _map_columns(row), table
    return None


def _map_columns(header):
    mapping = {}
    used_targets = set()
    for idx, cell in enumerate(header):
        cell_l = (cell or "").lower()
        for target, keyword_groups in KEYWORDS.items():
            if target in used_targets:
                continue
            if any(all(kw in cell_l for kw in group) for group in keyword_groups):
                mapping[idx] = target
                used_targets.add(target)
                break
    return mapping
