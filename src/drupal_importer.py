#!/usr/bin/env python3
"""
drupal_importer.py

One-time ETL import: reads the software inventory spreadsheet,
merges rows by unique vendor+product (unioning site codes from the
X-marked site columns), and POSTs one Drupal node per unique product.

Usage:
    python drupal_importer.py --dry-run               # preview without posting
    python drupal_importer.py                          # live import
    python drupal_importer.py --limit 10              # import first 10 records only
    python drupal_importer.py --spreadsheet /path/to/file.xlsx
    python drupal_importer.py --help

Required .env variables:
    DRUPAL_BASE_URL       e.g. http://bmi-capella.uhmc.sunysb.edu
    DRUPAL_USERNAME       dedicated scanner account
    DRUPAL_PASSWORD       scanner account password
    DRUPAL_CONTENT_TYPE   machine name of the content type
"""

import logging
import os
import sys
from pathlib import Path

import openpyxl
import requests
import urllib3
from dotenv import load_dotenv

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

DEFAULT_SPREADSHEET = Path.home() / "Documents" / "misc" / "software_inventory.xlsx"

# Sheets that are metadata/reference — not software rows
SKIP_SHEETS = {"MASTER Spreadsheet", "Priority Level Definitions", "Dropdowns", "Sheet1"}

# The 8 site columns in the spreadsheet (cell value "X" = used at that site)
SITE_COLUMNS = ["SBUH", "SBSH", "SBELIH", "CPMP", "SBAS", "HSC", "MHL", "SDM"]

# Spreadsheet column label (lowercase) → Drupal field machine name
FIELD_MAP = {
    "division":                          "field_division",
    "vendor name":                       "field_vendor_name",
    "status":                            "field_status",
    "product name":                      "field_product_name",
    "contains phi (yes/no)":             "field_contains_phi",
    "mission critical (yes/no)":         "field_mission_critical",
    "priority for business continuity":  "field_priority_for_business_cont",
    "business criticality level":        "field_business_criticality_level",
    "description":                       "field_description",
    "contract terms":                    "field_contract_terms",
    "certificate expiration date":       "field_certificate_expiration_dat",
    "responisible dept/category":        "field_responsible_dept_category",
    "business sponsor name/phone number": "field_business_sponsor_name_phon",
    "it director/ manager":              "field_it_director_manager",
    "it technical contact":              "field_it_technical_contact",
}


def parse_args() -> dict:
    """Parse command-line arguments from sys.argv."""
    args = sys.argv[1:]

    if "--help" in args or "-h" in args:
        print(__doc__)
        sys.exit(0)

    dry_run = "--dry-run" in args
    debug = "--debug" in args
    limit = None
    spreadsheet = DEFAULT_SPREADSHEET

    if "--limit" in args:
        idx = args.index("--limit")
        if idx + 1 < len(args):
            limit = int(args[idx + 1])

    if "--spreadsheet" in args:
        idx = args.index("--spreadsheet")
        if idx + 1 < len(args):
            spreadsheet = Path(args[idx + 1])

    return {"dry_run": dry_run, "debug": debug, "limit": limit, "spreadsheet": spreadsheet}


def load_config() -> dict:
    """Load and validate required environment variables from .env."""
    load_dotenv()
    required = ["DRUPAL_BASE_URL", "DRUPAL_USERNAME", "DRUPAL_PASSWORD", "DRUPAL_CONTENT_TYPE"]
    missing = [k for k in required if not os.environ.get(k)]
    if missing:
        logger.error("Missing required environment variables: %s", ", ".join(missing))
        sys.exit(1)
    return {
        "base_url": os.environ["DRUPAL_BASE_URL"].rstrip("/"),
        "username": os.environ["DRUPAL_USERNAME"],
        "password": os.environ["DRUPAL_PASSWORD"],
        "content_type": os.environ["DRUPAL_CONTENT_TYPE"],
    }


def clean(value) -> str:
    """Normalize a cell value to a stripped string; return empty string for blank or nan."""
    text = str(value or "").strip()
    return "" if text.lower() == "nan" else text


def load_and_merge(filepath: Path) -> list[dict]:
    """
    Read all data sheets, skipping metadata sheets.
    Rows with the same vendor+product are merged into one record:
      - field_sites_used is unioned across sheets
      - For all other fields, the first non-empty value wins; conflicts are logged
    Returns a list of merged records keyed by Drupal field names.
    """
    wb = openpyxl.load_workbook(filepath, data_only=True)
    merged = {}  # (vendor_lower, product_lower) → record dict

    for sheet_name in wb.sheetnames:
        if sheet_name.strip() in SKIP_SHEETS:
            continue

        ws = wb[sheet_name]
        rows = list(ws.iter_rows(values_only=True))
        if len(rows) < 2:
            logger.warning("Sheet '%s' is empty, skipping", sheet_name)
            continue

        header = [str(c).strip() if c else "" for c in rows[0]]
        header_lower = [h.lower() for h in header]

        # Vendor and product columns are required
        vendor_col = next(
            (i for i, h in enumerate(header_lower) if "vendor" in h and "name" in h), None
        )
        product_col = next(
            (i for i, h in enumerate(header_lower) if h == "product name"), None
        )
        if vendor_col is None or product_col is None:
            logger.warning("Sheet '%s' missing vendor/product columns, skipping", sheet_name)
            continue

        # Locate site columns (not all sheets have all 8)
        site_col_indices = {}
        for site in SITE_COLUMNS:
            if site in header:
                site_col_indices[site] = header.index(site)

        # Locate mapped field columns
        field_col_indices = {}
        for col_label, drupal_field in FIELD_MAP.items():
            if col_label in header_lower:
                field_col_indices[drupal_field] = header_lower.index(col_label)

        logger.info("Sheet '%s': %d data rows", sheet_name.strip(), len(rows) - 1)

        for row in rows[1:]:
            if len(row) <= max(vendor_col, product_col):
                continue

            vendor = clean(row[vendor_col])
            product = clean(row[product_col])
            if not vendor or not product:
                continue

            key = (vendor.lower(), product.lower())

            # Collect site codes where the cell contains "X"
            sites = {
                site
                for site, col_idx in site_col_indices.items()
                if col_idx < len(row) and clean(row[col_idx]).upper() == "X"
            }

            if key in merged:
                existing = merged[key]
                existing["field_sites_used"] |= sites

                # Log conflicts on other fields
                for drupal_field, col_idx in field_col_indices.items():
                    if drupal_field in ("field_vendor_name", "field_product_name"):
                        continue
                    new_val = clean(row[col_idx]) if col_idx < len(row) else ""
                    old_val = existing.get(drupal_field, "")
                    if new_val and old_val and new_val != old_val:
                        logger.warning(
                            "Conflict '%s — %s' [%s]: keeping '%s', ignoring '%s'",
                            vendor, product, drupal_field, old_val, new_val,
                        )
            else:
                record = {
                    "field_vendor_name": vendor,
                    "field_product_name": product,
                    "field_sites_used": sites,
                }
                for drupal_field, col_idx in field_col_indices.items():
                    val = clean(row[col_idx]) if col_idx < len(row) else ""
                    if val:
                        record[drupal_field] = val
                merged[key] = record

    logger.info("Total unique products after merging: %d", len(merged))
    return list(merged.values())


def build_payload(record: dict, content_type: str) -> dict:
    """Transform a merged record into a Drupal JSON:API POST payload."""
    vendor = record.get("field_vendor_name", "")
    product = record.get("field_product_name", "")

    attributes = {"title": f"{vendor} — {product}"}
    for field, value in record.items():
        if field == "field_sites_used":
            attributes[field] = sorted(value)  # array of site code strings
        elif value:
            attributes[field] = value

    return {
        "data": {
            "type": f"node--{content_type}",
            "attributes": attributes,
        }
    }


def post_node(session: requests.Session, base_url: str, content_type: str, payload: dict) -> bool:
    """POST a single node to Drupal via JSON:API. Returns True on success."""
    url = f"{base_url}/jsonapi/node/{content_type}"
    try:
        response = session.post(url, json=payload, timeout=30)
        response.raise_for_status()
        return True
    except requests.exceptions.HTTPError as exc:
        title = payload["data"]["attributes"].get("title", "?")
        logger.error(
            "HTTP %s posting '%s': %s",
            exc.response.status_code, title, exc.response.text[:300],
        )
        return False
    except requests.exceptions.RequestException as exc:
        logger.error("Request failed: %s", exc)
        return False


def main() -> None:
    args = parse_args()

    if args["debug"]:
        logging.getLogger().setLevel(logging.DEBUG)

    config = load_config()
    spreadsheet = args["spreadsheet"]

    if not spreadsheet.exists():
        logger.error("Spreadsheet not found: %s", spreadsheet)
        sys.exit(1)

    logger.info("Loading spreadsheet: %s", spreadsheet)
    records = load_and_merge(spreadsheet)

    if args["limit"]:
        records = records[: args["limit"]]
        logger.info("Limiting to first %d records", args["limit"])

    if args["dry_run"]:
        logger.info("DRY RUN — no nodes will be posted to Drupal")
        for record in records:
            sites = sorted(record.get("field_sites_used", set()))
            logger.info(
                "  %s — %s | sites: %s",
                record.get("field_vendor_name"),
                record.get("field_product_name"),
                sites,
            )
        logger.info("Would create %d node(s)", len(records))
        return

    session = requests.Session()
    session.auth = (config["username"], config["password"])
    session.headers.update({
        "Accept": "application/vnd.api+json",
        "Content-Type": "application/vnd.api+json",
    })
    # NOTE: verify=False is acceptable for internal university servers.
    # Remove before any public-facing deployment.
    session.verify = False
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    succeeded, failed = 0, 0
    total = len(records)

    for i, record in enumerate(records, 1):
        title = f"{record.get('field_vendor_name')} — {record.get('field_product_name')}"
        logger.info("[%d/%d] %s", i, total, title)
        payload = build_payload(record, config["content_type"])
        if post_node(session, config["base_url"], config["content_type"], payload):
            succeeded += 1
        else:
            failed += 1

    logger.info("Import complete: %d succeeded, %d failed", succeeded, failed)


if __name__ == "__main__":
    main()
