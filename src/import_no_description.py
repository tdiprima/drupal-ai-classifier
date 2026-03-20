#!/usr/bin/env python3
"""
import_no_description.py

Imports Drupal nodes for spreadsheet rows that have Vendor Name and Product Name
but a blank Description field — rows that drupal_importer.py currently skips.

Supports resuming interrupted imports via a progress file.

Usage:
    python import_no_description.py --dry-run               # preview without posting
    python import_no_description.py                         # live import (or resume)
    python import_no_description.py --limit 10              # import first N records only
    python import_no_description.py --reset                 # clear progress and start over
    python import_no_description.py --spreadsheet /path/to/file.xlsx
    python import_no_description.py --help

Required .env variables:
    DRUPAL_BASE_URL       e.g. http://bmi-capella.uhmc.sunysb.edu
    DRUPAL_USERNAME       dedicated scanner account
    DRUPAL_PASSWORD       scanner account password
    DRUPAL_CONTENT_TYPE   machine name of the content type
"""

import difflib
import json
import logging
import os
import sys
import time
from contextlib import suppress
from datetime import datetime
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
DEFAULT_PROGRESS_FILE = Path(__file__).parent.parent / "import_nodesc_progress.json"

SKIP_SHEETS = {"MASTER Spreadsheet", "Priority Level Definitions", "Dropdowns", "Sheet1"}
SITE_COLUMNS = ["SBUH", "SBSH", "SBELIH", "CPMP", "SBAS", "HSC", "MHL", "SDM", "SHH", "SOM"]

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

FIELD_ALLOWED_VALUES: dict[str, set] = {
    "field_ai_application":             {"yes", "no"},
    "field_business_criticality_level": {"core_infrastructure", "critical", "high", "medium", "low"},
    "field_confidence":                 {"high", "medium", "low"},
    "field_contains_phi":               {"yes", "no"},
    "field_division":                   {"sbuh", "sbsh", "sbelih", "cpmp", "sbas", "hsc", "mhl", "sdm", "shh", "som"},
    "field_mission_critical":           {"yes", "no"},
    "field_priority_for_business_cont": {1, 2, 3, 4, 5, 6, 7, 8, 9, 10},
    "field_sites_used":                 {"sbuh", "sbsh", "sbelih", "cpmp", "sbas", "hsc", "mhl", "sdm", "shh", "som"},
    "field_status":                     {"active", "inactive"},
}

FIELD_INTEGER_FIELDS: set[str] = {"field_priority_for_business_cont"}
DATE_FIELDS: set[str] = {"field_certificate_expiration_dat"}

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
    """Load the set of already-imported (vendor, product) keys from disk."""
    if not progress_file.exists():
        return set()
    try:
        data = json.loads(progress_file.read_text(encoding="utf-8"))
        return {tuple(entry) for entry in data.get("completed", [])}
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Could not read progress file %s: %s — starting fresh", progress_file, exc)
        return set()


def save_progress(progress_file: Path, completed: set, run_stats: dict) -> None:
    """Write completed keys and run stats to disk atomically."""
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
    """Add a key to the completed set and flush to disk."""
    completed.add(key)
    save_progress(progress_file, completed, run_stats)


# ---------------------------------------------------------------------------
# Spreadsheet loading
# ---------------------------------------------------------------------------

def clean(value) -> str:
    """Normalize a cell value to a stripped string; return empty string for blank or nan."""
    text = str(value or "").strip()
    return "" if text.lower() == "nan" else text


def load_no_description_records(filepath: Path) -> list[dict]:
    """
    Read all data sheets and return merged records for rows that have
    Vendor Name and Product Name but a blank Description.

    Rows are merged by vendor+product key the same way as drupal_importer.py.
    """
    wb = openpyxl.load_workbook(filepath, data_only=True)
    merged: dict[tuple, dict] = {}

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

        desc_col = field_col_indices.get("field_description")
        found_in_sheet = 0

        for row in rows[1:]:
            if len(row) <= max(vendor_col, product_col):
                continue

            vendor = clean(row[vendor_col])
            product = clean(row[product_col])

            if not vendor or not product:
                continue

            description = clean(row[desc_col]) if (desc_col is not None and desc_col < len(row)) else ""
            if description:
                # Has a description — handled by the main importer
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
                    if drupal_field in ("field_vendor_name", "field_product_name", "field_description"):
                        continue
                    new_val = clean(row[col_idx]) if col_idx < len(row) else ""
                    old_val = existing.get(drupal_field, "")
                    if new_val and old_val and new_val != old_val:
                        logger.warning(
                            "Conflict '%s — %s' [%s]: keeping '%s', ignoring '%s'",
                            vendor, product, drupal_field, old_val, new_val,
                        )
            else:
                record: dict = {
                    "field_vendor_name": vendor,
                    "field_product_name": product,
                    "field_sites_used": sites,
                }
                for drupal_field, col_idx in field_col_indices.items():
                    if drupal_field == "field_description":
                        continue
                    val = clean(row[col_idx]) if col_idx < len(row) else ""
                    if val:
                        record[drupal_field] = val
                merged[key] = record
                found_in_sheet += 1

        if found_in_sheet:
            logger.info("Sheet '%s': %d no-description row(s)", sheet_name.strip(), found_in_sheet)

    logger.info("Total unique no-description products: %d", len(merged))
    return list(merged.values())


# ---------------------------------------------------------------------------
# Drupal interaction  (mirrors drupal_importer.py)
# ---------------------------------------------------------------------------

def normalize_list_value(field: str, raw: str) -> str | int | None:
    """Match a raw value to the exact key Drupal expects for a list field."""
    allowed = FIELD_ALLOWED_VALUES.get(field)
    if allowed is None:
        return raw

    if field in FIELD_INTEGER_FIELDS:
        with suppress(ValueError, TypeError):
            raw_int = int(float(raw))
            if raw_int in allowed:
                return raw_int
        return None

    if raw in allowed:
        return raw
    raw_lower = raw.lower()
    for allowed_val in allowed:
        if allowed_val.lower() == raw_lower:
            return allowed_val

    candidates = list(allowed)
    close = difflib.get_close_matches(raw_lower, [c.lower() for c in candidates], n=1, cutoff=0.8)
    if close:
        matched = next(c for c in candidates if c.lower() == close[0])
        logger.warning("Fuzzy-matched %r → %r for %s", raw, matched, field)
        return matched

    return None


def normalize_date_value(raw: str) -> str | None:
    """Coerce a date value to YYYY-MM-DD."""
    if not raw:
        return None
    date_part = raw.split(" ")[0].split("T")[0]
    if len(date_part) == 10 and date_part[4] == date_part[7] == "-":
        return date_part
    return None


def build_payload(record: dict, content_type: str) -> dict:
    """Transform a record into a Drupal JSON:API POST payload."""
    vendor = record.get("field_vendor_name", "")
    product = record.get("field_product_name", "")

    attributes: dict = {"title": f"{vendor} \u2014 {product}"}

    for field, value in record.items():
        if field == "field_sites_used":
            allowed = FIELD_ALLOWED_VALUES["field_sites_used"]
            valid_sites = [s.lower() for s in sorted(value) if s.lower() in allowed]
            invalid_sites = [s for s in value if s.lower() not in allowed]
            if invalid_sites:
                logger.warning(
                    "'%s — %s': unrecognized site(s) for field_sites_used: %s — skipping",
                    vendor, product, invalid_sites,
                )
            attributes[field] = [{"value": site} for site in valid_sites]
        elif not value:
            continue
        elif field in DATE_FIELDS:
            normalized = normalize_date_value(str(value))
            if normalized:
                attributes[field] = normalized
            else:
                logger.warning(
                    "'%s — %s': unrecognized date value for %s: %r — skipping field",
                    vendor, product, field, value,
                )
        elif field in FIELD_ALLOWED_VALUES:
            normalized = normalize_list_value(field, str(value))
            if normalized is not None:
                attributes[field] = normalized
            else:
                logger.warning(
                    "'%s — %s': unrecognized value for %s: %r — skipping field",
                    vendor, product, field, value,
                )
        else:
            attributes[field] = value

    return {
        "data": {
            "type": f"node--{content_type}",
            "attributes": attributes,
        }
    }


def extract_invalid_fields(error_body: str) -> list[str]:
    """Parse a Drupal 422 response and return field names rejected as invalid choices."""
    try:
        data = json.loads(error_body)
    except (json.JSONDecodeError, ValueError):
        return []

    invalid_fields = []
    for error in data.get("errors", []):
        detail = error.get("detail", "")
        pointer = error.get("source", {}).get("pointer", "")
        if "not a valid choice" in detail and pointer:
            parts = pointer.strip("/").split("/")
            if len(parts) >= 3 and parts[0] == "data" and parts[1] == "attributes":
                invalid_fields.append(parts[2])
    return invalid_fields


def post_node_with_retry(
    session: requests.Session,
    base_url: str,
    content_type: str,
    payload: dict,
) -> bool:
    """
    POST a node to Drupal with retries.
    On 422 caused by invalid list-field values, strips those fields and retries once.
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
            body = exc.response.text[:800]
            if status == 422:
                invalid = extract_invalid_fields(exc.response.text)
                if invalid:
                    logger.warning(
                        "HTTP 422 posting '%s' — dropping invalid field(s) and retrying: %s",
                        title, invalid,
                    )
                    for field in invalid:
                        payload["data"]["attributes"].pop(field, None)
                    continue
                logger.error("HTTP 422 posting '%s' (not retrying): %s", title, body)
                return False
            if 400 <= status < 500:
                logger.error("HTTP %s posting '%s' (not retrying): %s", status, title, body)
                return False
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
            logger.warning("Timeout posting '%s' (attempt %d/%d)", title, attempt, MAX_RETRIES)

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

    if args["reset"]:
        if progress_file.exists():
            progress_file.unlink()
            logger.info("Progress file cleared — starting from scratch")
        else:
            logger.info("No progress file found — nothing to reset")

    logger.info("Loading spreadsheet: %s", spreadsheet)
    records = load_no_description_records(spreadsheet)

    if args["limit"]:
        records = records[: args["limit"]]
        logger.info("Limiting to first %d records", args["limit"])

    total = len(records)

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
            title = f"{vendor} \u2014 {product}"
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
        logger.info("IMPORT (NO DESCRIPTION) SUMMARY")
        logger.info("  Total found          : %d", total)
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
