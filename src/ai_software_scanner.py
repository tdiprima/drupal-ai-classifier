#!/usr/bin/env python3
"""
AI Software Scanner (OpenAI)

Reads an Excel file of approved software and uses OpenAI to determine
which ones contain embedded AI features that need security review.

Usage:
    python ai_software_scanner.py software_inventory.xlsx  # Uses MASTER Spreadsheet
    python ai_software_scanner.py software_inventory.xlsx --sheet "Dental School"  # Specific sheet
    python ai_software_scanner.py software_inventory.xlsx --all  # All sheets
    python ai_software_scanner.py software_inventory.xlsx --debug  # Show AI responses

Output:
    - Console summary
    - ai_scan_results.csv with all results
"""

import logging
import sys
from datetime import datetime
from pathlib import Path

from openai import OpenAI

from ai_scanner_core import (configure_logging, load_software_list,
                             save_results, scan_software)

logger = logging.getLogger(__name__)


def parse_args() -> tuple:
    """Parse command-line arguments from sys.argv."""
    if len(sys.argv) < 2:
        print(
            "Usage: python ai_software_scanner.py <file.xlsx> [--sheet NAME | --all | --debug]",
            file=sys.stderr,
        )
        sys.exit(1)

    input_file = sys.argv[1]
    sheet_name = None
    all_sheets = "--all" in sys.argv
    debug = "--debug" in sys.argv

    if "--sheet" in sys.argv:
        idx = sys.argv.index("--sheet")
        if idx + 1 < len(sys.argv):
            sheet_name = sys.argv[idx + 1]

    return input_file, sheet_name, all_sheets, debug


def main():
    input_file, sheet_name, all_sheets, debug = parse_args()
    configure_logging(debug)

    if not Path(input_file).exists():
        logger.error("File '%s' not found", input_file)
        sys.exit(1)

    logger.info("Loading from %s...", input_file)
    software_list = load_software_list(input_file, sheet_name, all_sheets)
    logger.info("Found %d software entries", len(software_list))

    if not software_list:
        logger.error("No software found")
        sys.exit(1)

    client = OpenAI()
    model = "gpt-5.2"

    results, flagged = scan_software(client, model, software_list)

    output_file = "ai_scan_results.csv"
    save_results(results, output_file)

    logger.info("=" * 60)
    logger.info("SCAN COMPLETE - %d checked, %d flagged", len(results), len(flagged))
    logger.info("Results saved to: %s", output_file)
    logger.info("Completed: %s", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))


if __name__ == "__main__":
    main()
