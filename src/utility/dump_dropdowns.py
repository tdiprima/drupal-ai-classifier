#!/usr/bin/env python3
"""
dump_dropdowns.py

Prints the contents of reference/metadata sheets from the spreadsheet
so you can see the allowed values for each list field.

Usage:
    python dump_dropdowns.py
    python dump_dropdowns.py /path/to/file.xlsx
"""

import sys
from pathlib import Path

import openpyxl

DEFAULT_SPREADSHEET = Path.home() / "Documents" / "misc" / "software_inventory.xlsx"
REFERENCE_SHEETS = {"Dropdowns", "Priority Level Definitions"}


def dump_sheet(ws) -> None:
    print(f"  Dimensions: {ws.dimensions}")
    rows = [row for row in ws.iter_rows(values_only=True) if any(c is not None for c in row)]
    for row_idx, row in enumerate(rows, 1):
        cells = "\t".join("" if cell is None else str(cell) for cell in row)
        print(f"  row {row_idx}:\t{cells}")


def main() -> None:
    filepath = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_SPREADSHEET

    if not filepath.exists():
        print(f"ERROR: file not found: {filepath}", file=sys.stderr)
        sys.exit(1)

    wb = openpyxl.load_workbook(filepath, data_only=True)

    for sheet_name in REFERENCE_SHEETS:
        if sheet_name not in wb.sheetnames:
            print(f"[{sheet_name}] — not found\n")
            continue
        print(f"[{sheet_name}]")
        dump_sheet(wb[sheet_name])
        print()


if __name__ == "__main__":
    main()
