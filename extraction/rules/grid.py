"""Rule-based parser for already-tabular input (PDF tables, xlsx, csv) whose
header row resembles our target schema closely enough to map column-by-column.
This is what handles a source that already looks like "a spreadsheet of
listings" — e.g. a broker's PDF export of their availability grid.
"""
from extraction.schema import COLUMNS

# Fuzzy keyword sets per target column — a header cell matches a column if
# ALL of its keyword-group's words appear in the (lowercased) header text.
KEYWORDS = {
    "Area": [["area"]],
    "Building": [["building"]],
    "Floor/Unit": [["floor", "unit"], ["floor/unit"]],
    "Size (sq ft)": [["size"], ["sq", "ft"], ["sqft"]],
    "Desks (max)": [["desk"]],
    "Marketing Price (Based on Min Term) PCM": [["pcm"]],
    "Marketing Price (Based on Min Term) PSF": [["psf"]],
    "Link to Brochure": [["brochure"]],
    "Min. Term": [["min", "term"]],
    "Special Features": [["special"], ["feature"]],
    "State of Space": [["state"]],
    "Legal Structure": [["legal"]],
    "Broker Fee": [["broker", "fee"]],
    "Floor Plan": [["floor", "plan"]],
    "High Res Images": [["high", "res"], ["images"]],
}
MIN_MATCHES = 4


def detect(content):
    return _find_table(content) is not None


def parse(content):
    table = _find_table(content)
    if not table:
        return []
    header, rows = table[0], table[1:]
    col_map = _map_columns(header)

    # "Contact 1"/"Contact 2": Kitt's-style sheets have one merged header
    # ("...team assigned to this space") over two name columns with no
    # sub-header text of their own — claim the first two unmapped columns
    # that sit to the right of "Broker Fee" (or, failing that, the last two
    # unmapped columns before any link-ish trailing columns).
    contact_cols = _guess_contact_columns(header, col_map)
    if contact_cols:
        col_map[contact_cols[0]] = "Contact 1"
        if len(contact_cols) > 1:
            col_map[contact_cols[1]] = "Contact 2"

    records = []
    for row in rows:
        if not any(c.strip() for c in row):
            continue
        record = {}
        for idx, col_name in col_map.items():
            if idx < len(row):
                record[col_name] = row[idx]
        if record.get("Building") or record.get("Area"):
            records.append(record)
    return records


def _find_table(content):
    for table in content.get("tables", []):
        if len(table) < 2:
            continue
        header = table[0]
        if len(_map_columns(header)) >= MIN_MATCHES:
            return table
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


def _guess_contact_columns(header, col_map):
    mapped_idxs = set(col_map.keys())
    unmapped = [i for i in range(len(header)) if i not in mapped_idxs]
    if not unmapped:
        return []
    # Prefer unmapped columns that come after Broker Fee, if we found one.
    broker_idx = next((i for i, c in col_map.items() if c == "Broker Fee"), None)
    if broker_idx is not None:
        after = [i for i in unmapped if i > broker_idx]
        if len(after) >= 2:
            return after[:2]
    return unmapped[:2]
