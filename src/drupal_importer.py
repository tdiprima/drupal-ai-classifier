#!/usr/bin/env python3
"""
drupal_importer.py

One-time ETL import: reads the software inventory spreadsheet,
merges rows by unique vendor+product (unioning site codes from the
X-marked site columns), and POSTs one Drupal node per unique product.

Supports resuming interrupted imports — already-imported records are
tracked in a progress file and skipped on subsequent runs.

Usage:
    python drupal_importer.py --dry-run               # preview without posting
    python drupal_importer.py                          # live import (or resume)
    python drupal_importer.py --limit 10              # import first N records only
    python drupal_importer.py --reset                 # clear progress and start over
    python drupal_importer.py --spreadsheet /path/to/file.xlsx
    python drupal_importer.py --progress-file /path/to/progress.json
    python drupal_importer.py --help

Required .env variables:
    DRUPAL_BASE_URL       e.g. http://bmi-capella.uhmc.sunysb.edu
    DRUPAL_USERNAME       dedicated scanner account
    DRUPAL_PASSWORD       scanner account password
    DRUPAL_CONTENT_TYPE   machine name of the content type
"""

import json
import logging
import os
import sys
import time
import urllib3
from datetime import datetime
from pathlib import Path

import openpyxl
import requests
from dotenv import load_dotenv

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

DEFAULT_SPREADSHEET = Path.home() / "Documents" / "misc" / "software_inventory.xlsx"
DEFAULT_PROGRESS_FILE = Path(__file__).parent.parent / "import_progress.json"

# Sheets that are metadata/reference — not software rows
SKIP_SHEETS = {"MASTER Spreadsheet", "Priority Level Definitions", "Dropdowns", "Sheet1"}

# The 8 site columns in the spreadsheet (cell value "X" = used at that site)
SITE_COLUMNS = ["SBUH", "SBSH", "SBELIH", "CPMP", "SBAS", "HSC", "MHL", "SDM"]

# Spreadsheet column label (lowercase) → Drupal field machine name
FIELD_MAP = {
    "division":                           "field_division",
    "vendor name":                        "field_vendor_name",
    "status":                             "field_status",
    "product name":                       "field_product_name",
    "contains phi (yes/no)":              "field_contains_phi",
    "mission critical (yes/no)":          "field_mission_critical",
    "priority for business continuity":   "field_priority_for_business_cont",
    "business criticality level":         "field_business_criticality_level",
    "description":                        "field_description",
    "contract terms":                     "field_contract_terms",
    "certificate expiration date":        "field_certificate_expiration_dat",
    "responisible dept/category":         "field_responsible_dept_category",
    "business sponsor name/phone number": "field_business_sponsor_name_phon",
    "it director/ manager":               "field_it_director_manager",
    "it technical contact":               "field_it_technical_contact",
}

# How many times to retry a failed POST before giving up
MAX_RETRIES = 3
RETRY_DELAY_SECONDS = 5


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

def parse_args() -> dict:
    """Parse command-line arguments from sys.argv."""
    args = sys.argv[1:]

    if "--help" in args or "-h" in args:
        print(__doc__)
        sys.exit(0)

    dry_run = "--dry-run" in args
    debug = "--debug" in args
    reset = "--reset" in args
    limit = None
    spreadsheet = DEFAULT_SPREADSHEET
    progress_file = DEFAULT_PROGRESS_FILE

    if "--limit" in args:
        idx = args.index("--limit")
        if idx + 1 < len(args):
            try:
                limit = int(args[idx + 1])
            except ValueError:
                logger.error("--limit requires an integer argument")
                sys.exit(1)

    if "--spreadsheet" in args:
        idx = args.index("--spreadsheet")
        if idx + 1 < len(args):
            spreadsheet = Path(args[idx + 1])

    if "--progress-file" in args:
        idx = args.index("--progress-file")
        if idx + 1 < len(args):
            progress_file = Path(args[idx + 1])

    return {
        "dry_run": dry_run,
        "debug": debug,
        "reset": reset,
        "limit": limit,
        "spreadsheet": spreadsheet,
        "progress_file": progress_file,
    }


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Progress tracking
# ---------------------------------------------------------------------------

def load_progress(progress_file: Path) -> set:
    """
    Load the set of already-imported (vendor, product) keys from disk.
    Returns an empty set if the file does not exist.
    """
    if not progress_file.exists():
        return set()
    try:
        data = json.loads(progress_file.read_text(encoding="utf-8"))
        return {tuple(entry) for entry in data.get("completed", [])}
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Could not read progress file %s: %s — starting fresh", progress_file, exc)
        return set()


def save_progress(progress_file: Path, completed: set, run_stats: dict) -> None:
    """
    Write the current set of completed keys and run stats to disk.
    Overwrites the file atomically via a temp file to avoid corruption.
    """
    data = {
        "last_updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "completed_count": len(completed),
        "completed": [list(key) for key in sorted(completed)],
        "last_run": run_stats,
    }
    tmp = progress_file.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(progress_file)
    except OSError as exc:
        logger.error("Failed to write progress file %s: %s", progress_file, exc)


def mark_complete(progress_file: Path, completed: set, key: tuple, run_stats: dict) -> None:
    """Add a key to the completed set and immediately flush to disk."""
    completed.add(key)
    save_progress(progress_file, completed, run_stats)


# ---------------------------------------------------------------------------
# Spreadsheet loading
# ---------------------------------------------------------------------------

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

        vendor_col = next(
            (i for i, h in enumerate(header_lower) if "vendor" in h and "name" in h), None
        )
        product_col = next(
            (i for i, h in enumerate(header_lower) if h == "product name"), None
        )
        if vendor_col is None or product_col is None:
            logger.warning("Sheet '%s' missing vendor/product columns, skipping", sheet_name)
            continue

        site_col_indices = {
            site: header.index(site)
            for site in SITE_COLUMNS
            if site in header
        }

        field_col_indices = {
            drupal_field: header_lower.index(col_label)
            for col_label, drupal_field in FIELD_MAP.items()
            if col_label in header_lower
        }

        logger.info("Sheet '%s': %d data rows", sheet_name.strip(), len(rows) - 1)

        for row in rows[1:]:
            if len(row) <= max(vendor_col, product_col):
                continue

            vendor = clean(row[vendor_col])
            product = clean(row[product_col])

            desc_col = field_col_indices.get("field_description")
            description = clean(row[desc_col]) if (desc_col is not None and desc_col < len(row)) else ""

            if not vendor or not product or not description:
                missing = [f for f, v in [("vendor", vendor), ("product", product), ("description", description)] if not v]
                logger.warning(
                    "Sheet '%s': skipping row — missing required field(s): %s",
                    sheet_name, ", ".join(missing),
                )
                continue

            key = (vendor.lower(), product.lower())

            sites = {
                site
                for site, col_idx in site_col_indices.items()
                if col_idx < len(row) and clean(row[col_idx]).upper() == "X"
            }

            if key in merged:
                existing = merged[key]
                existing["field_sites_used"] |= sites
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


# ---------------------------------------------------------------------------
# Drupal interaction
# ---------------------------------------------------------------------------

def build_payload(record: dict, content_type: str) -> dict:
    """Transform a merged record into a Drupal JSON:API POST payload."""
    vendor = record.get("field_vendor_name", "")
    product = record.get("field_product_name", "")

    attributes = {"title": f"{vendor} — {product}"}
    for field, value in record.items():
        if field == "field_sites_used":
            attributes[field] = sorted(value)
        elif value:
            attributes[field] = value

    return {
        "data": {
            "type": f"node--{content_type}",
            "attributes": attributes,
        }
    }


def post_node_with_retry(
    session: requests.Session,
    base_url: str,
    content_type: str,
    payload: dict,
) -> bool:
    """
    POST a single node to Drupal, retrying on transient connection errors.
    Returns True on success, False if all attempts fail.
    4xx errors are not retried — they indicate a data or config problem.
    """
    url = f"{base_url}/jsonapi/node/{content_type}"
    title = payload["data"]["attributes"].get("title", "?")

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = session.post(url, json=payload, timeout=30)
            response.raise_for_status()
            return True

        except requests.exceptions.HTTPError as exc:
            status = exc.response.status_code
            body = exc.response.text[:400]
            if 400 <= status < 500:
                # Client error — retrying won't help
                logger.error(
                    "HTTP %s posting '%s' (not retrying): %s", status, title, body
                )
                return False
            # Server error — worth retrying
            logger.warning(
                "HTTP %s posting '%s' (attempt %d/%d): %s",
                status, title, attempt, MAX_RETRIES, body,
            )

        except requests.exceptions.ConnectionError as exc:
            logger.warning(
                "Connection error posting '%s' (attempt %d/%d): %s",
                title, attempt, MAX_RETRIES, exc,
            )

        except requests.exceptions.Timeout:
            logger.warning(
                "Timeout posting '%s' (attempt %d/%d)", title, attempt, MAX_RETRIES
            )

        if attempt < MAX_RETRIES:
            logger.info("Retrying in %ds...", RETRY_DELAY_SECONDS)
            time.sleep(RETRY_DELAY_SECONDS)

    logger.error("All %d attempts failed for '%s'", MAX_RETRIES, title)
    return False


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    args = parse_args()

    if args["debug"]:
        logging.getLogger().setLevel(logging.DEBUG)

    config = load_config()
    spreadsheet = args["spreadsheet"]
    progress_file = args["progress_file"]

    if not spreadsheet.exists():
        logger.error("Spreadsheet not found: %s", spreadsheet)
        sys.exit(1)

    # Handle --reset before loading progress
    if args["reset"]:
        if progress_file.exists():
            progress_file.unlink()
            logger.info("Progress file cleared — starting from scratch")
        else:
            logger.info("No progress file found — nothing to reset")

    logger.info("Loading spreadsheet: %s", spreadsheet)
    records = load_and_merge(spreadsheet)

    if args["limit"]:
        records = records[: args["limit"]]
        logger.info("Limiting to first %d records", args["limit"])

    total = len(records)

    # -----------------------------------------------------------------------
    # Dry run
    # -----------------------------------------------------------------------
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
        logger.info("Would create %d node(s)", total)
        return

    # -----------------------------------------------------------------------
    # Live import
    # -----------------------------------------------------------------------
    completed = load_progress(progress_file)
    already_done = len(completed)
    if already_done:
        logger.info(
            "Resuming: %d record(s) already imported, %d remaining",
            already_done,
            total - already_done,
        )

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

    succeeded = 0
    failed = 0
    skipped = 0
    start_time = time.monotonic()

    run_stats = {
        "started": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "succeeded": 0,
        "failed": 0,
        "skipped": 0,
    }

    try:
        for i, record in enumerate(records, 1):
            vendor = record.get("field_vendor_name", "")
            product = record.get("field_product_name", "")
            title = f"{vendor} — {product}"
            key = (vendor.lower(), product.lower())

            if key in completed:
                logger.debug("[%d/%d] SKIP (already imported): %s", i, total, title)
                skipped += 1
                continue

            logger.info("[%d/%d] %s", i, total, title)
            payload = build_payload(record, config["content_type"])

            if post_node_with_retry(session, config["base_url"], config["content_type"], payload):
                succeeded += 1
                run_stats.update({"succeeded": succeeded, "failed": failed, "skipped": skipped})
                mark_complete(progress_file, completed, key, run_stats)
                logger.debug("  Saved to progress file")
            else:
                failed += 1
                run_stats.update({"succeeded": succeeded, "failed": failed, "skipped": skipped})
                logger.error("  FAILED — will retry on next run")

    except KeyboardInterrupt:
        logger.warning("Import interrupted by user (Ctrl+C)")

    finally:
        elapsed = time.monotonic() - start_time
        run_stats.update({
            "succeeded": succeeded,
            "failed": failed,
            "skipped": skipped,
            "finished": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "elapsed_seconds": round(elapsed, 1),
        })
        save_progress(progress_file, completed, run_stats)

        logger.info("=" * 60)
        logger.info("IMPORT SUMMARY")
        logger.info("  Total in spreadsheet : %d", total)
        logger.info("  Skipped (done prior) : %d", skipped)
        logger.info("  Succeeded this run   : %d", succeeded)
        logger.info("  Failed this run      : %d", failed)
        logger.info("  Still to import      : %d", total - skipped - succeeded)
        logger.info("  Elapsed              : %.1fs", elapsed)
        logger.info("  Progress file        : %s", progress_file)
        if failed:
            logger.warning("Re-run this script to retry the %d failed record(s)", failed)


if __name__ == "__main__":
    main()
