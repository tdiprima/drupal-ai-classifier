#!/usr/bin/env python3
"""
drupal_dedup_patch.py

Repairs nodes whose data was partially corrupted by the deduplication process.

The dedup kept the node with more fields filled in and deleted the other.
But it never checked whether the *values* of the shared fields matched.
If they differed, the "loser" node was a distinct record — and the data
unique to it is now lost in the surviving node.

This script treats the XLSX as ground truth and, for every node that still
exists in Drupal, compares each field value against the XLSX source.
Where Drupal is empty and XLSX has a value, or where the values disagree
in a material way, it builds a targeted PATCH to restore the lost data.

What this does NOT do:
  - Delete anything
  - Re-create missing nodes (use drupal_recovery.py for that)
  - Overwrite a Drupal value with an identical one (no-ops are skipped)

Usage:
    python drupal_dedup_patch.py --dry-run          # preview — no PATCHes
    python drupal_dedup_patch.py                     # live patching
    python drupal_dedup_patch.py --limit 10          # first N records only
    python drupal_dedup_patch.py --spreadsheet /path/to/file.xlsx
    python drupal_dedup_patch.py --help

Required .env variables (same as drupal_importer.py):
    DRUPAL_BASE_URL, DRUPAL_USERNAME, DRUPAL_PASSWORD, DRUPAL_CONTENT_TYPE
"""

import logging
import sys
import time
from pathlib import Path

import requests
import urllib3

sys.path.insert(0, str(Path(__file__).parent))
from drupal_importer import (DATE_FIELDS, DEFAULT_SPREADSHEET,
                             FIELD_ALLOWED_VALUES, load_config,
                             normalize_date_value, normalize_list_value)
from drupal_patcher import fetch_all_nodes, patch_node
from drupal_recovery import build_expected_records

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# How many nodes to fetch per JSON:API page (matches drupal_patcher.py)
PAGE_SIZE = 50

# Fields we attempt to restore from XLSX if Drupal is empty or mismatched.
# This mirrors PATCH_FIELDS in drupal_patcher.py but covers all mapped fields.
RESTORABLE_FIELDS = {
    "field_vendor_name",
    "field_product_name",
    "field_division",
    "field_status",
    "field_contains_phi",
    "field_mission_critical",
    "field_priority_for_business_cont",
    "field_business_criticality_level",
    "field_description",
    "field_contract_terms",
    "field_certificate_expiration_dat",
    "field_responsible_dept_category",
    "field_business_sponsor_name_phon",
    "field_it_director_manager",
    "field_it_technical_contact",
    "field_sites_used",
}


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
    limit = None
    spreadsheet = DEFAULT_SPREADSHEET

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

    return {
        "dry_run": dry_run,
        "debug": debug,
        "limit": limit,
        "spreadsheet": spreadsheet,
    }


# ---------------------------------------------------------------------------
# Fetch a single node's full attributes
# ---------------------------------------------------------------------------

def fetch_node_attributes(
    session: requests.Session,
    base_url: str,
    content_type: str,
    uuid: str,
    title: str,
) -> dict | None:
    """
    Fetch all field attributes for a single node by UUID.
    Returns the attributes dict, or None on failure.
    """
    url = f"{base_url}/jsonapi/node/{content_type}/{uuid}"
    try:
        response = session.get(url, timeout=30)
        response.raise_for_status()
        return response.json().get("data", {}).get("attributes", {})
    except requests.exceptions.RequestException as exc:
        logger.error("Failed to fetch node '%s' (%s): %s", title, uuid, exc)
        return None


# ---------------------------------------------------------------------------
# Field-value comparison
# ---------------------------------------------------------------------------

def normalize_for_comparison(field: str, value) -> str:
    """
    Produce a canonical lowercase string for a Drupal field value so that
    two values can be compared regardless of formatting differences.

    For list fields (multi-value), returns a sorted comma-separated string.
    """
    if value is None:
        return ""
    if isinstance(value, list):
        # JSON:API multi-value: [{"value": "..."}, ...]
        parts = sorted(
            str(item.get("value", item)).strip().lower()
            if isinstance(item, dict) else str(item).strip().lower()
            for item in value
        )
        return ",".join(parts)
    if isinstance(value, set):
        return ",".join(sorted(str(s).strip().lower() for s in value))
    return str(value).strip().lower()


def xlsx_value_for_field(record: dict, field: str) -> str:
    """
    Return the normalized XLSX value for a field so it can be compared
    against the Drupal value from normalize_for_comparison().
    """
    value = record.get(field)
    if value is None:
        return ""
    if field == "field_sites_used":
        return normalize_for_comparison(field, value)
    if field in DATE_FIELDS:
        normalized = normalize_date_value(str(value))
        return normalized.lower() if normalized else ""
    if field in FIELD_ALLOWED_VALUES:
        normalized = normalize_list_value(field, str(value))
        return str(normalized).lower() if normalized is not None else ""
    return str(value).strip().lower()


def fields_are_equivalent(field: str, xlsx_record: dict, drupal_attrs: dict) -> bool:
    """
    Return True if the XLSX value and the Drupal value for this field are
    materially the same (case-insensitive, format-normalized).
    """
    xlsx_norm = xlsx_value_for_field(xlsx_record, field)
    drupal_norm = normalize_for_comparison(field, drupal_attrs.get(field))
    return xlsx_norm == drupal_norm


# ---------------------------------------------------------------------------
# Build PATCH payload for differing fields
# ---------------------------------------------------------------------------

def build_restoration_attributes(
    record: dict,
    drupal_attrs: dict,
    title: str,
) -> dict:
    """
    Compare each restorable field between the XLSX record and the live
    Drupal node. Return a dict of attributes that need to be patched:

      - Field is in XLSX but empty in Drupal → restore from XLSX
      - Field has a value in both but they differ → restore from XLSX
        (XLSX is ground truth; the dedup may have kept the wrong copy)
      - Field is empty in XLSX → skip (nothing to restore)
      - Field values are equivalent → skip (no-op)
    """
    attributes: dict = {}
    record.get("field_vendor_name", "")
    record.get("field_product_name", "")

    for field in RESTORABLE_FIELDS:
        xlsx_val = record.get(field)

        # Nothing in XLSX — nothing to restore
        if not xlsx_val and xlsx_val != 0:
            continue

        # Values already match — no patch needed
        if fields_are_equivalent(field, record, drupal_attrs):
            continue

        drupal_norm = normalize_for_comparison(field, drupal_attrs.get(field))
        xlsx_norm = xlsx_value_for_field(record, field)  # for logging

        if drupal_norm:
            logger.info(
                "  CONFLICT  %-45s | drupal: %r  xlsx: %r",
                field, drupal_norm, xlsx_norm,
            )
        else:
            logger.info(
                "  MISSING   %-45s | xlsx: %r",
                field, xlsx_norm,
            )

        # Build the corrected value in Drupal JSON:API format
        if field == "field_sites_used":
            allowed = FIELD_ALLOWED_VALUES["field_sites_used"]
            sites = xlsx_val if isinstance(xlsx_val, set) else set()
            valid = [s.lower() for s in sorted(sites) if s.lower() in allowed]
            invalid = [s for s in sites if s.lower() not in allowed]
            if invalid:
                logger.warning(
                    "'%s': unrecognized site(s) %s — skipping those", title, invalid
                )
            attributes[field] = [{"value": s} for s in valid]

        elif field in DATE_FIELDS:
            normalized = normalize_date_value(str(xlsx_val))
            if normalized:
                attributes[field] = normalized
            else:
                logger.warning(
                    "'%s' [%s]: date parse failed for %r — skipping",
                    title, field, xlsx_val,
                )

        elif field in FIELD_ALLOWED_VALUES:
            normalized = normalize_list_value(field, str(xlsx_val))
            if normalized is not None:
                attributes[field] = normalized
            else:
                logger.warning(
                    "'%s' [%s]: value %r not in allowed set — skipping",
                    title, field, xlsx_val,
                )

        else:
            attributes[field] = xlsx_val

    return attributes


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    args = parse_args()

    if args["debug"]:
        logging.getLogger().setLevel(logging.DEBUG)

    config = load_config()
    spreadsheet: Path = args["spreadsheet"]

    if not spreadsheet.exists():
        logger.error("Spreadsheet not found: %s", spreadsheet)
        sys.exit(1)

    # -----------------------------------------------------------------
    # Step 1: authoritative records from XLSX (both import phases)
    # -----------------------------------------------------------------
    logger.info("Loading spreadsheet: %s", spreadsheet)
    expected = build_expected_records(spreadsheet)

    # -----------------------------------------------------------------
    # Step 2: title → UUID index from Drupal
    # -----------------------------------------------------------------
    session = requests.Session()
    session.auth = (config["username"], config["password"])
    session.headers.update({
        "Accept": "application/vnd.api+json",
        "Content-Type": "application/vnd.api+json",
    })
    session.verify = False
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    logger.info("Fetching node index from Drupal...")
    node_index = fetch_all_nodes(session, config["base_url"], config["content_type"])
    logger.info("Indexed %d nodes in Drupal", len(node_index))

    # -----------------------------------------------------------------
    # Step 3: compare XLSX vs Drupal field-by-field
    # -----------------------------------------------------------------
    records = list(expected.values())
    if args["limit"]:
        records = records[: args["limit"]]
        logger.info("Limiting to first %d records", args["limit"])

    total = len(records)
    patched = 0
    skipped_clean = 0
    skipped_missing = 0
    failed = 0
    start_time = time.monotonic()

    for i, record in enumerate(records, 1):
        vendor = record.get("field_vendor_name", "")
        product = record.get("field_product_name", "")
        title = f"{vendor} \u2014 {product}"

        uuid = node_index.get(title)
        if not uuid:
            # Node is completely absent — drupal_recovery.py handles this
            logger.debug("[%d/%d] NOT IN DRUPAL (skipping — use drupal_recovery.py): %s", i, total, title)
            skipped_missing += 1
            continue

        logger.debug("[%d/%d] Checking: %s", i, total, title)

        drupal_attrs = fetch_node_attributes(
            session, config["base_url"], config["content_type"], uuid, title
        )
        if drupal_attrs is None:
            failed += 1
            continue

        logger.info("[%d/%d] %s", i, total, title)
        attributes = build_restoration_attributes(record, drupal_attrs, title)

        if not attributes:
            logger.debug("  All fields match — nothing to patch")
            skipped_clean += 1
            continue

        if args["dry_run"]:
            logger.info(
                "  DRY RUN — would patch %d field(s): %s",
                len(attributes), list(attributes.keys()),
            )
            patched += 1  # count as "would patch" for the summary
            continue

        payload = {
            "data": {
                "type": f"node--{config['content_type']}",
                "id": uuid,
                "attributes": attributes,
            }
        }

        if patch_node(
            session, config["base_url"], config["content_type"], uuid, payload, title
        ):
            logger.info("  Patched %d field(s)", len(attributes))
            patched += 1
        else:
            failed += 1

    elapsed = time.monotonic() - start_time
    dry_label = " (DRY RUN)" if args["dry_run"] else ""

    logger.info("=" * 60)
    logger.info("DEDUP PATCH SUMMARY%s", dry_label)
    logger.info("  Total XLSX records       : %d", total)
    logger.info("  Patched (data restored)  : %d", patched)
    logger.info("  Already correct          : %d", skipped_clean)
    logger.info("  Not in Drupal (missing)  : %d", skipped_missing)
    logger.info("  Fetch/patch errors       : %d", failed)
    logger.info("  Elapsed                  : %.1fs", elapsed)
    if skipped_missing:
        logger.info(
            "  Run drupal_recovery.py to re-create the %d missing node(s)",
            skipped_missing,
        )


if __name__ == "__main__":
    main()
