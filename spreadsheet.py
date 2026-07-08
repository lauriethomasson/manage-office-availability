"""Load, de-duplicate/append, and write the consolidated master spreadsheet."""
from pathlib import Path

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from extraction.schema import COLUMNS, dedup_key, normalize_record

HEADER_FILL = PatternFill(start_color="FF1F2937", end_color="FF1F2937", fill_type="solid")
HEADER_FONT = Font(bold=True, color="FFFFFFFF")
CURRENCY_COLS = {"Marketing Price (Based on Min Term) PCM", "Marketing Price (Based on Min Term) PSF"}
NUMBER_COLS = {"Size (sq ft)", "Desks (max)"}
LINK_COLS = {"Link to Brochure", "Floor Plan", "High Res Images"}


def load_master(path):
    """Returns a list of record dicts from an existing master spreadsheet,
    or [] if the file doesn't exist yet."""
    path = Path(path)
    if not path.exists():
        return []
    wb = load_workbook(path, data_only=True)
    ws = wb.active
    rows = list(ws.iter_rows(values_only=True))
    if not rows:
        return []
    header = [str(c) if c is not None else "" for c in rows[0]]
    records = []
    for row in rows[1:]:
        if not any(c not in (None, "") for c in row):
            continue
        record = {col: row[i] if i < len(row) else "" for i, col in enumerate(header)}
        records.append(normalize_record(record))
    return records


def merge_records(existing, new_records):
    """Upserts new_records into existing by dedup_key — a re-uploaded
    building/floor overwrites the old row (availability data is assumed to
    be current-state, not historical), everything else is appended."""
    by_key = {dedup_key(r): r for r in existing}
    order = [dedup_key(r) for r in existing]
    for r in new_records:
        k = dedup_key(r)
        if k not in by_key:
            order.append(k)
        by_key[k] = r
    return [by_key[k] for k in order]


def write_xlsx(path, records):
    wb = Workbook()
    ws = wb.active
    ws.title = "Master"

    ws.append(COLUMNS)
    for cell in ws[1]:
        cell.font = HEADER_FONT
        cell.fill = HEADER_FILL
        cell.alignment = Alignment(vertical="center")
    ws.freeze_panes = "A2"

    for record in records:
        row = [record.get(c, "") for c in COLUMNS]
        ws.append(row)

    last_row = ws.max_row
    for col_idx, col_name in enumerate(COLUMNS, start=1):
        letter = get_column_letter(col_idx)
        max_len = len(col_name)
        for row_idx in range(2, last_row + 1):
            cell = ws.cell(row=row_idx, column=col_idx)
            val = cell.value
            if col_name in CURRENCY_COLS and isinstance(val, (int, float)) and val != "":
                cell.number_format = "£#,##0.00" if col_name.endswith("PSF") else "£#,##0"
            elif col_name in NUMBER_COLS and isinstance(val, (int, float)) and val != "":
                cell.number_format = "#,##0"
            elif col_name in LINK_COLS and isinstance(val, str) and val.startswith("http"):
                cell.hyperlink = val
                cell.font = Font(color="FF0563C1", underline="single")
            max_len = max(max_len, len(str(val)) if val is not None else 0)
        ws.column_dimensions[letter].width = min(max(max_len + 2, 10), 45)

    Path(path).parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)
