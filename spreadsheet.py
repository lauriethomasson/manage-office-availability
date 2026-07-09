"""Writes a single source's extracted records to a formatted .xlsx."""
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from extraction.schema import COLUMNS

HEADER_FILL = PatternFill(start_color="FF1F2937", end_color="FF1F2937", fill_type="solid")
HEADER_FONT = Font(bold=True, color="FFFFFFFF")
CURRENCY_COLS = {"Marketing Price (Based on Min Term) PCM", "Marketing Price (Based on Min Term) PSF"}
NUMBER_COLS = {"Size (sq ft)", "Desks (max)"}
COORDINATE_COLS = {"Lat", "Lng"}
LINK_COLS = {"Link to Brochure", "Floor Plan", "High Res Images"}


def write_xlsx(path, records, sheet_title="Listings"):
    wb = Workbook()
    ws = wb.active
    ws.title = sheet_title[:31] or "Listings"  # Excel sheet name length limit

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
            elif col_name in COORDINATE_COLS and isinstance(val, (int, float)) and val != "":
                cell.number_format = "0.000000"
            elif col_name in LINK_COLS and isinstance(val, str) and val.startswith("http"):
                cell.hyperlink = val
                cell.font = Font(color="FF0563C1", underline="single")
            max_len = max(max_len, len(str(val)) if val is not None else 0)
        ws.column_dimensions[letter].width = min(max(max_len + 2, 10), 45)

    Path(path).parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)
