#!/usr/bin/env python3
"""
drupal_recovery.py

Identifies Drupal nodes that were incorrectly deleted by the deduplication
process and re-imports them from the XLSX source.

Strategy:
  1. Build the authoritative expected key set from the XLSX (both phases:
     rows with a description AND rows without).
  2. Fetch every node currently in Drupal.
  3. Diff the two sets to find missing (vendor, product) pairs.
  4. Re-import only those missing records, skipping anything already present.

Uses a dedicated recovery progress file so the run is safely resumable.

Usage:
    python drupal_recovery.py --dry-run               # preview — no POSTs
    python drupal_recovery.py                          # live recovery
    python drupal_recovery.py --limit 10               # process first N missing
    python drupal_recovery.py --reset                  # clear recovery progress
    python drupal_recovery.py --spreadsheet /path/to/file.xlsx
    python drupal_recovery.py --help

Required .env variables (same as drupal_importer.py):
    DRUPAL_BASE_URL, DRUPAL_USERNAME, DRUPAL_PASSWORD, DRUPAL_CONTENT_TYPE
"""

import logging
import sys
import time
from datetime import datetime
from pathlib import Path

import requests
import urllib3

sys.path.insert(0, str(Path(__file__).parent))
from drupal_importer import (DEFAULT_SPREADSHEET, build_payload,
                             load_and_merge, load_config, load_progress,
                             mark_complete, post_node_with_retry,
                             save_progress)
from import_no_description import load_no_description_records

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

DEFAULT_PROGRESS_FILE = Path(__file__).parent.parent / "recovery_progress.json"

# How many nodes to fetch per JSON:API page
PAGE_SIZE = 50


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
# XLSX — build authoritative key set
# ---------------------------------------------------------------------------

def build_expected_records(spreadsheet: Path) -> dict[tuple, dict]:
    """
    Return a mapping of (vendor_lower, product_lower) → record for every
    unique product in the XLSX, covering both import phases:

      - Phase 1: rows that have a description  (drupal_importer.py)
      - Phase 2: rows without a description    (import_no_description.py)

    Phase-1 records win when the same key appears in both phases
    (they carry more data).
    """
    phase1 = {
        (r["field_vendor_name"].lower(), r["field_product_name"].lower()): r
        for r in load_and_merge(spreadsheet)
    }
    phase2 = {
        (r["field_vendor_name"].lower(), r["field_product_name"].lower()): r
        for r in load_no_description_records(spreadsheet)
    }

    # Merge: phase1 takes precedence; phase2 fills in keys not covered by phase1
    combined = {**phase2, **phase1}

    logger.info(
        "Expected records — phase1: %d, phase2: %d, combined unique: %d",
        len(phase1),
        len(phase2),
        len(combined),
    )
    return combined


# ---------------------------------------------------------------------------
# Drupal — fetch all current nodes
# ---------------------------------------------------------------------------

def fetch_present_keys(
    session: requests.Session,
    base_url: str,
    content_type: str,
) -> set[tuple]:
    """
    Fetch all existing Drupal nodes of content_type and return their
    (vendor_lower, product_lower) keys, derived from the node title.

    Node titles were created as  "{vendor} — {product}"  (em dash U+2014).
    """
    present: set[tuple] = {}
    url = f"{base_url}/jsonapi/node/{content_type}"
    params = {
        f"fields[node--{content_type}]": "title",
        "page[limit]": PAGE_SIZE,
    }

    page = 0
    while url:
        page += 1
        try:
            response = session.get(url, params=params if page == 1 else None, timeout=30)
            response.raise_for_status()
        except requests.exceptions.RequestException as exc:
            logger.error("Failed to fetch node list (page %d): %s", page, exc)
            sys.exit(1)

        data = response.json()
        for node in data.get("data", []):
            title = node.get("attributes", {}).get("title", "")
            key = _key_from_title(title)
            if key:
                present[key] = title

        logger.info("  Fetched page %d (%d nodes so far)", page, len(present))
        url = data.get("links", {}).get("next", {}).get("href")
        params = None  # subsequent pages use the encoded next-link

    logger.info("Total nodes currently in Drupal: %d", len(present))
    return set(present.keys())


def _key_from_title(title: str) -> tuple | None:
    """
    Extract (vendor_lower, product_lower) from a node title.
    Titles are stored as  "Vendor Name — Product Name"  (em dash).
    Returns None if the title cannot be parsed.
    """
    em_dash = "\u2014"
    if em_dash not in title:
        return None
    vendor, _, product = title.partition(em_dash)
    vendor = vendor.strip()
    product = product.strip()
    if not vendor or not product:
        return None
    return (vendor.lower(), product.lower())


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    args = parse_args()

    if args["debug"]:
        logging.getLogger().setLevel(logging.DEBUG)

    config = load_config()
    spreadsheet: Path = args["spreadsheet"]
    progress_file: Path = args["progress_file"]

    if not spreadsheet.exists():
        logger.error("Spreadsheet not found: %s", spreadsheet)
        sys.exit(1)

    if args["reset"]:
        if progress_file.exists():
            progress_file.unlink()
            logger.info("Recovery progress file cleared")
        else:
            logger.info("No recovery progress file found — nothing to reset")

    # -----------------------------------------------------------------
    # Step 1: build the authoritative expected key → record mapping
    # -----------------------------------------------------------------
    logger.info("Loading spreadsheet: %s", spreadsheet)
    expected = build_expected_records(spreadsheet)

    # -----------------------------------------------------------------
    # Step 2: fetch what's actually in Drupal right now
    # -----------------------------------------------------------------
    session = requests.Session()
    session.auth = (config["username"], config["password"])
    session.headers.update({
        "Accept": "application/vnd.api+json",
        "Content-Type": "application/vnd.api+json",
    })
    session.verify = False
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    logger.info("Fetching all current nodes from Drupal...")
    present_keys = fetch_present_keys(session, config["base_url"], config["content_type"])

    # -----------------------------------------------------------------
    # Step 3: diff — find what was deleted
    # -----------------------------------------------------------------
    missing_keys = set(expected.keys()) - present_keys
    logger.info(
        "Gap analysis — expected: %d, present: %d, missing: %d",
        len(expected),
        len(present_keys),
        len(missing_keys),
    )

    if not missing_keys:
        logger.info("No missing nodes detected — nothing to recover.")
        return

    missing_records = [expected[k] for k in sorted(missing_keys)]

    if args["limit"]:
        missing_records = missing_records[: args["limit"]]
        logger.info("Limiting recovery to first %d record(s)", args["limit"])

    total = len(missing_records)

    # -----------------------------------------------------------------
    # Dry run
    # -----------------------------------------------------------------
    if args["dry_run"]:
        logger.info("DRY RUN — no nodes will be re-imported")
        for record in missing_records:
            logger.info(
                "  MISSING: %s — %s",
                record["field_vendor_name"],
                record["field_product_name"],
            )
        logger.info("Would re-import %d node(s)", total)
        return

    # -----------------------------------------------------------------
    # Step 4: re-import missing nodes
    # -----------------------------------------------------------------
    completed = load_progress(progress_file)
    already_done = len(completed)
    if already_done:
        logger.info("Resuming: %d record(s) already recovered, skipping them", already_done)

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
        for i, record in enumerate(missing_records, 1):
            vendor = record.get("field_vendor_name", "")
            product = record.get("field_product_name", "")
            title = f"{vendor} \u2014 {product}"
            key = (vendor.lower(), product.lower())

            if key in completed:
                logger.debug("[%d/%d] SKIP (already recovered): %s", i, total, title)
                skipped += 1
                continue

            logger.info("[%d/%d] Recovering: %s", i, total, title)
            payload = build_payload(record, config["content_type"])

            if post_node_with_retry(
                session, config["base_url"], config["content_type"], payload
            ):
                succeeded += 1
                run_stats.update({"succeeded": succeeded, "failed": failed, "skipped": skipped})
                mark_complete(progress_file, completed, key, run_stats)
            else:
                failed += 1
                run_stats.update({"succeeded": succeeded, "failed": failed, "skipped": skipped})
                logger.error("  FAILED — will retry on next run")

    except KeyboardInterrupt:
        logger.warning("Recovery interrupted by user (Ctrl+C)")

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
        logger.info("RECOVERY SUMMARY")
        logger.info("  Missing (gap identified) : %d", total)
        logger.info("  Skipped (done prior)     : %d", skipped)
        logger.info("  Recovered this run       : %d", succeeded)
        logger.info("  Failed this run          : %d", failed)
        logger.info("  Still to recover         : %d", total - skipped - succeeded)
        logger.info("  Elapsed                  : %.1fs", elapsed)
        logger.info("  Progress file            : %s", progress_file)
        if failed:
            logger.warning("Re-run this script to retry the %d failed record(s)", failed)


if __name__ == "__main__":
    main()
