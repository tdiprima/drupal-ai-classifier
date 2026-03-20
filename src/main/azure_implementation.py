#!/usr/bin/env python3
"""
AI Software Scanner (Azure OpenAI)

Reads an Excel file of approved software and uses Azure OpenAI to determine
which ones contain embedded AI features that need security review.

Usage:
    python azure_implementation.py software_inventory.xlsx  # Uses MASTER Spreadsheet
    python azure_implementation.py software_inventory.xlsx --sheet "Dental School"  # Specific sheet
    python azure_implementation.py software_inventory.xlsx --all  # All sheets
    python azure_implementation.py software_inventory.xlsx --debug  # Show AI responses

Output:
    - Console summary
    - ai_scan_results.csv with all results

Environment Variables Required:
    AZURE_OPENAI_ENDPOINT
    AZURE_OPENAI_API_KEY
    AZURE_OPENAI_DEPLOYMENT
"""

import logging
import os
import sys
from datetime import datetime
from pathlib import Path

from openai import AzureOpenAI

from ai_scanner_core import (configure_logging, load_software_list,
                             save_results, scan_software)

logger = logging.getLogger(__name__)


def parse_args() -> tuple:
    """Parse command-line arguments from sys.argv."""
    if len(sys.argv) < 2:
        print(
            "Usage: python azure_implementation.py <file.xlsx> [--sheet NAME | --all | --debug]",
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


def load_azure_config() -> tuple[str, str, str]:
    """Load and validate Azure OpenAI configuration from environment variables."""
    required_vars = ["AZURE_OPENAI_ENDPOINT", "AZURE_OPENAI_API_KEY", "AZURE_OPENAI_DEPLOYMENT"]
    missing = [var for var in required_vars if not os.environ.get(var)]

    if missing:
        for var in missing:
            logger.error("Missing required environment variable: %s", var)
        sys.exit(1)

    return (
        os.environ["AZURE_OPENAI_ENDPOINT"],
        os.environ["AZURE_OPENAI_API_KEY"],
        os.environ["AZURE_OPENAI_DEPLOYMENT"],
    )


def main():
    input_file, sheet_name, all_sheets, debug = parse_args()
    configure_logging(debug)

    endpoint, api_key, deployment = load_azure_config()

    if not Path(input_file).exists():
        logger.error("File '%s' not found", input_file)
        sys.exit(1)

    logger.info("Loading from %s...", input_file)
    software_list = load_software_list(input_file, sheet_name, all_sheets)
    logger.info("Found %d software entries", len(software_list))

    if not software_list:
        logger.error("No software found")
        sys.exit(1)

    client = AzureOpenAI(
        azure_endpoint=endpoint,
        api_key=api_key,
        api_version="2024-02-15-preview",
    )

    results, flagged = scan_software(client, deployment, software_list)

    output_file = "ai_scan_results.csv"
    save_results(results, output_file)

    logger.info("=" * 60)
    logger.info("SCAN COMPLETE - %d checked, %d flagged", len(results), len(flagged))
    logger.info("Results saved to: %s", output_file)
    logger.info("Completed: %s", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))


if __name__ == "__main__":
    main()
