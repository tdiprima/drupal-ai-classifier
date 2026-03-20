#!/usr/bin/env python3
"""
patch_skipped.py

Targeted patch for the "Import skipped" cases identified by audit_importer.py.

Reads audit_report.csv, collects every row with status "Import skipped",
and PATCHes those specific fields on the corresponding Drupal nodes.
No spreadsheet re-read needed — the audit CSV already contains the
exact node/field/value triples that need to be written.

Usage:
    python patch_skipped.py --dry-run
    python patch_skipped.py
    python patch_skipped.py --audit-csv /path/to/audit_report.csv
    python patch_skipped.py --help

Required .env variables: same as drupal_importer.py
    DRUPAL_BASE_URL, DRUPAL_USERNAME, DRUPAL_PASSWORD, DRUPAL_CONTENT_TYPE
"""

import csv
import logging
import sys
import time
from collections import defaultdict
from pathlib import Path

import requests
import urllib3

sys.path.insert(0, str(Path(__file__).parent))
from drupal_importer import (DATE_FIELDS, FIELD_ALLOWED_VALUES, load_config,
                             normalize_date_value, normalize_list_value)
from drupal_patcher import fetch_all_nodes

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

STATUS_SKIPPED = "Import skipped"
DEFAULT_AUDIT_CSV = Path(__file__).parent.parent / "src" / "audit_report.csv"
PAGE_SIZE = 50


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

def parse_args() -> dict:
    """Parse command-line arguments."""
    args = sys.argv[1:]

    if "--help" in args or "-h" in args:
        print(__doc__)
        sys.exit(0)

    dry_run = "--dry-run" in args
    audit_csv = DEFAULT_AUDIT_CSV

    if "--audit-csv" in args:
        idx = args.index("--audit-csv")
        if idx + 1 < len(args):
            audit_csv = Path(args[idx + 1])

    return {"dry_run": dry_run, "audit_csv": audit_csv}


# ---------------------------------------------------------------------------
# Read audit CSV
# ---------------------------------------------------------------------------

def load_skipped_patches(audit_csv: Path) -> dict[str, dict[str, str]]:
    """
    Read audit_report.csv and collect every "Import skipped" row.
    Returns a dict mapping node_id → {drupal_field: excel_value}.
    """
    if not audit_csv.exists():
        logger.error("Audit CSV not found: %s", audit_csv)
        sys.exit(1)

    patches: dict[str, dict[str, str]] = defaultdict(dict)
    total_rows = 0

    with audit_csv.open(encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            if row["status"] != STATUS_SKIPPED:
                continue
            node_id = row["node_id"]
            drupal_field = row["drupal_field"]
            excel_value = row["excel_value"]
            if excel_value:
                patches[node_id][drupal_field] = excel_value
                total_rows += 1

    logger.info(
        "Found %d skipped field(s) across %d node(s) to patch",
        total_rows,
        len(patches),
    )
    return patches.copy()


# ---------------------------------------------------------------------------
# Build PATCH attributes for a single node
# ---------------------------------------------------------------------------

def build_patch_attributes(
    node_id: str,
    field_values: dict[str, str],
) -> dict:
    """
    Normalize each field value the same way the importer would and return
    a Drupal JSON:API attributes dict ready for a PATCH request.
    Fields that still fail normalization are logged and skipped.
    """
    attributes: dict = {}

    for drupal_field, raw_value in field_values.items():
        if not raw_value:
            continue

        if drupal_field == "field_sites_used":
            # excel_value is a comma-separated string like "cpmp,sbuh"
            allowed = FIELD_ALLOWED_VALUES["field_sites_used"]
            sites = [s.strip().lower() for s in raw_value.split(",") if s.strip()]
            valid = [s for s in sites if s in allowed]
            invalid = [s for s in sites if s not in allowed]
            if invalid:
                logger.warning("'%s' [%s]: unrecognized site(s) %s — skipping those", node_id, drupal_field, invalid)
            attributes[drupal_field] = [{"value": s} for s in sorted(valid)]
            continue

        if drupal_field in DATE_FIELDS:
            normalized = normalize_date_value(raw_value)
            if normalized:
                attributes[drupal_field] = normalized
            else:
                logger.warning(
                    "'%s' [%s]: date parse failed for %r — skipping",
                    node_id, drupal_field, raw_value,
                )
            continue

        if drupal_field in FIELD_ALLOWED_VALUES:
            normalized = normalize_list_value(drupal_field, raw_value)
            if normalized is not None:
                attributes[drupal_field] = normalized
            else:
                logger.warning(
                    "'%s' [%s]: value %r not in allowed set — skipping",
                    node_id, drupal_field, raw_value,
                )
            continue

        # Plain text field — pass through as-is
        attributes[drupal_field] = raw_value

    return attributes


# ---------------------------------------------------------------------------
# PATCH a single node
# ---------------------------------------------------------------------------

def patch_node(
    session: requests.Session,
    base_url: str,
    content_type: str,
    uuid: str,
    payload: dict,
    title: str,
) -> bool:
    """Send a PATCH request for one node. Returns True on success."""
    url = f"{base_url}/jsonapi/node/{content_type}/{uuid}"
    try:
        response = session.patch(url, json=payload, timeout=30)
        response.raise_for_status()
        return True
    except requests.exceptions.HTTPError as exc:
        logger.error(
            "HTTP %s patching '%s': %s",
            exc.response.status_code, title, exc.response.text[:400],
        )
    except requests.exceptions.RequestException as exc:
        logger.error("Request error patching '%s': %s", title, exc)
    return False


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    args = parse_args()
    config = load_config()

    patches = load_skipped_patches(args["audit_csv"])
    if not patches:
        logger.info("No 'Import skipped' rows found — nothing to patch.")
        return

    session = requests.Session()
    session.auth = (config["username"], config["password"])
    session.headers.update({
        "Accept": "application/vnd.api+json",
        "Content-Type": "application/vnd.api+json",
    })
    session.verify = False

    if args["dry_run"]:
        logger.info("DRY RUN — no nodes will be patched")
        for node_id, field_values in sorted(patches.items()):
            attrs = build_patch_attributes(node_id, field_values)
            logger.info("  Would patch: %s", node_id)
            for field, value in attrs.items():
                logger.info("    %-45s = %r", field, value)
        return

    logger.info("Fetching node index from Drupal...")
    node_index = fetch_all_nodes(session, config["base_url"], config["content_type"])
    logger.info("Indexed %d nodes", len(node_index))

    total = len(patches)
    patched = 0
    failed = 0
    skipped = 0
    start_time = time.monotonic()

    for idx, (node_id, field_values) in enumerate(sorted(patches.items()), 1):
        uuid = node_index.get(node_id)
        if not uuid:
            logger.warning("[%d/%d] Node not found in Drupal — skipping: %s", idx, total, node_id)
            skipped += 1
            continue

        attributes = build_patch_attributes(node_id, field_values)
        if not attributes:
            logger.warning("[%d/%d] No valid attributes to patch for: %s", idx, total, node_id)
            skipped += 1
            continue

        logger.info("[%d/%d] Patching: %s", idx, total, node_id)
        for field, value in attributes.items():
            logger.info("  %-45s = %r", field, value)

        payload = {
            "data": {
                "type": f"node--{config['content_type']}",
                "id": uuid,
                "attributes": attributes,
            }
        }

        if patch_node(session, config["base_url"], config["content_type"], uuid, payload, node_id):
            patched += 1
        else:
            failed += 1

    elapsed = time.monotonic() - start_time
    logger.info("=" * 60)
    logger.info("PATCH SUMMARY")
    logger.info("  Total nodes     : %d", total)
    logger.info("  Patched         : %d", patched)
    logger.info("  Skipped         : %d", skipped)
    logger.info("  Failed          : %d", failed)
    logger.info("  Elapsed         : %.1fs", elapsed)


if __name__ == "__main__":
    main()
