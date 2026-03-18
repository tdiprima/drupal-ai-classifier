#!/usr/bin/env python3
"""
drupal_patcher.py

Reads the spreadsheet, finds each already-imported node in Drupal by title,
and PATCHes fields that were missing or incorrect during the initial import
(list fields that needed lowercase keys, field_sites_used format, etc.).

Usage:
    python drupal_patcher.py --dry-run       # preview without patching
    python drupal_patcher.py                  # live patch
    python drupal_patcher.py --limit 10       # patch first N records only
    python drupal_patcher.py --help

Required .env variables: same as drupal_importer.py
"""

import logging
import os
import sys
import time
import urllib3
from pathlib import Path

import requests
from dotenv import load_dotenv

# Reuse spreadsheet loading and field normalization from the importer
sys.path.insert(0, str(Path(__file__).parent))
from drupal_importer import (
    load_and_merge,
    normalize_list_value,
    normalize_date_value,
    FIELD_ALLOWED_VALUES,
    FIELD_INTEGER_FIELDS,
    DATE_FIELDS,
    DEFAULT_SPREADSHEET,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Fields to patch — the ones affected by the key/format fixes
PATCH_FIELDS = {
    "field_ai_application",
    "field_business_criticality_level",
    "field_confidence",
    "field_contains_phi",
    "field_division",
    "field_mission_critical",
    "field_sites_used",
    "field_status",
    "field_certificate_expiration_dat",
}

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

    return {"dry_run": dry_run, "limit": limit, "spreadsheet": spreadsheet}


# ---------------------------------------------------------------------------
# Drupal node index
# ---------------------------------------------------------------------------

def fetch_all_nodes(session: requests.Session, base_url: str, content_type: str) -> dict[str, str]:
    """
    Fetch all nodes of the given content type and return a
    {title: uuid} mapping. Handles JSON:API pagination automatically.
    """
    index: dict[str, str] = {}
    url = f"{base_url}/jsonapi/node/{content_type}"
    params = {
        "fields[node--{ct}]".replace("{ct}", content_type): "title",
        "page[limit]": PAGE_SIZE,
    }

    page = 0
    while url:
        page += 1
        response = session.get(url, params=params if page == 1 else None, timeout=30)
        if response.status_code != 200:
            logger.error("Failed to fetch node list (HTTP %s)", response.status_code)
            sys.exit(1)

        data = response.json()
        for node in data.get("data", []):
            title = node.get("attributes", {}).get("title", "")
            uuid = node.get("id", "")
            if title and uuid:
                index[title] = uuid

        logger.info("  Fetched page %d (%d nodes so far)", page, len(index))

        # Follow the next page link if present
        url = data.get("links", {}).get("next", {}).get("href")
        params = None  # params are already encoded in the next link

    return index


# ---------------------------------------------------------------------------
# Payload building
# ---------------------------------------------------------------------------

def build_patch_attributes(record: dict) -> dict:
    """
    Build the attributes dict for a PATCH request, including only the
    fields in PATCH_FIELDS that have a valid normalized value.
    """
    vendor = record.get("field_vendor_name", "")
    product = record.get("field_product_name", "")
    attributes: dict = {}

    for field in PATCH_FIELDS:
        value = record.get(field)

        if field == "field_sites_used":
            sites = value or set()
            allowed = FIELD_ALLOWED_VALUES["field_sites_used"]
            valid = [s.lower() for s in sorted(sites) if s.lower() in allowed]
            invalid = [s for s in sites if s.lower() not in allowed]
            if invalid:
                logger.warning(
                    "'%s — %s': unrecognized site(s): %s — skipping", vendor, product, invalid
                )
            attributes[field] = [{"value": s} for s in valid]
            continue

        if not value:
            continue

        if field in DATE_FIELDS:
            normalized = normalize_date_value(str(value))
            if normalized:
                attributes[field] = normalized
            else:
                logger.warning(
                    "'%s — %s': unrecognized date for %s: %r — skipping", vendor, product, field, value
                )
            continue

        if field in FIELD_ALLOWED_VALUES:
            normalized = normalize_list_value(field, str(value))
            if normalized is not None:
                attributes[field] = normalized
            else:
                logger.warning(
                    "'%s — %s': unrecognized value for %s: %r — skipping", vendor, product, field, value
                )
            continue

    return attributes


def build_patch_payload(content_type: str, uuid: str, attributes: dict) -> dict:
    """Wrap attributes in a JSON:API PATCH payload."""
    return {
        "data": {
            "type": f"node--{content_type}",
            "id": uuid,
            "attributes": attributes,
        }
    }


# ---------------------------------------------------------------------------
# Patching
# ---------------------------------------------------------------------------

def patch_node(
    session: requests.Session,
    base_url: str,
    content_type: str,
    uuid: str,
    payload: dict,
    title: str,
) -> bool:
    """PATCH a single node. Returns True on success."""
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

    load_dotenv()
    required = ["DRUPAL_BASE_URL", "DRUPAL_USERNAME", "DRUPAL_PASSWORD", "DRUPAL_CONTENT_TYPE"]
    missing = [k for k in required if not os.environ.get(k)]
    if missing:
        logger.error("Missing required environment variables: %s", ", ".join(missing))
        sys.exit(1)

    base_url = os.environ["DRUPAL_BASE_URL"].rstrip("/")
    username = os.environ["DRUPAL_USERNAME"]
    password = os.environ["DRUPAL_PASSWORD"]
    content_type = os.environ["DRUPAL_CONTENT_TYPE"]
    spreadsheet = args["spreadsheet"]

    if not spreadsheet.exists():
        logger.error("Spreadsheet not found: %s", spreadsheet)
        sys.exit(1)

    session = requests.Session()
    session.auth = (username, password)
    session.headers.update({
        "Accept": "application/vnd.api+json",
        "Content-Type": "application/vnd.api+json",
    })
    session.verify = False

    logger.info("Loading spreadsheet: %s", spreadsheet)
    records = load_and_merge(spreadsheet)

    if args["limit"]:
        records = records[: args["limit"]]
        logger.info("Limiting to first %d records", args["limit"])

    if args["dry_run"]:
        logger.info("DRY RUN — no nodes will be patched")
        for record in records:
            vendor = record.get("field_vendor_name", "")
            product = record.get("field_product_name", "")
            attrs = build_patch_attributes(record)
            logger.info("  Would patch '%s — %s': %s", vendor, product, list(attrs.keys()))
        return

    logger.info("Fetching all existing nodes from Drupal (this may take a moment)...")
    node_index = fetch_all_nodes(session, base_url, content_type)
    logger.info("Indexed %d nodes", len(node_index))

    total = len(records)
    patched = 0
    skipped = 0
    failed = 0
    start_time = time.monotonic()

    for i, record in enumerate(records, 1):
        vendor = record.get("field_vendor_name", "")
        product = record.get("field_product_name", "")
        title = f"{vendor} \u2014 {product}"

        uuid = node_index.get(title)
        if not uuid:
            logger.warning("[%d/%d] Node not found in Drupal, skipping: %s", i, total, title)
            skipped += 1
            continue

        attributes = build_patch_attributes(record)
        if not attributes:
            logger.debug("[%d/%d] No patchable fields for: %s", i, total, title)
            skipped += 1
            continue

        logger.info("[%d/%d] Patching: %s", i, total, title)
        payload = build_patch_payload(content_type, uuid, attributes)

        if patch_node(session, base_url, content_type, uuid, payload, title):
            patched += 1
        else:
            failed += 1

    elapsed = time.monotonic() - start_time
    logger.info("=" * 60)
    logger.info("PATCH SUMMARY")
    logger.info("  Total records   : %d", total)
    logger.info("  Patched         : %d", patched)
    logger.info("  Skipped         : %d", skipped)
    logger.info("  Failed          : %d", failed)
    logger.info("  Elapsed         : %.1fs", elapsed)


if __name__ == "__main__":
    main()
