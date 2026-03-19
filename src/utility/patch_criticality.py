#!/usr/bin/env python3
"""
patch_criticality.py

Reads to_be_fixed.csv and patches field_business_criticality_level
to "critical" for each node listed.

Usage:
    python patch_criticality.py --dry-run       # preview without patching
    python patch_criticality.py                 # live patch
    python patch_criticality.py --help

Required .env variables: same as drupal_importer.py
"""

import csv
import logging
import os
import sys
import time
import urllib3

import requests
from dotenv import load_dotenv

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

PAGE_SIZE = 50
CSV_FILE = os.path.join(os.path.dirname(__file__), "..", "to_be_fixed.csv")
PATCH_VALUE = "critical"

# The mojibake em dash from the CSV vs the real em dash in Drupal titles
MOJIBAKE_DASH = "\u201a\u00c4\u00ee"
REAL_DASH = "\u2014"


def parse_args() -> dict:
    """Parse command-line arguments from sys.argv."""
    args = sys.argv[1:]

    if "--help" in args or "-h" in args:
        print(__doc__)
        sys.exit(0)

    return {"dry_run": "--dry-run" in args}


def fetch_all_nodes(
    session: requests.Session, base_url: str, content_type: str
) -> dict[str, str]:
    """Fetch all nodes and return a {title: uuid} mapping."""
    index: dict[str, str] = {}
    url = f"{base_url}/jsonapi/node/{content_type}"
    params = {
        f"fields[node--{content_type}]": "title",
        "page[limit]": PAGE_SIZE,
    }

    page = 0
    while url:
        page += 1
        response = session.get(
            url, params=params if page == 1 else None, timeout=30
        )
        if response.status_code != 200:
            logger.error(
                "Failed to fetch node list (HTTP %s)", response.status_code
            )
            sys.exit(1)

        data = response.json()
        for node in data.get("data", []):
            title = node.get("attributes", {}).get("title", "")
            uuid = node.get("id", "")
            if title and uuid:
                index[title] = uuid

        logger.info("  Fetched page %d (%d nodes so far)", page, len(index))

        url = data.get("links", {}).get("next", {}).get("href")
        params = None

    return index


def read_csv_titles(csv_path: str) -> list[str]:
    """Read the CSV and return a list of node titles, fixing mojibake."""
    titles = []
    with open(csv_path, newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            raw_title = row["node_id"].strip()
            title = raw_title.replace(MOJIBAKE_DASH, REAL_DASH)
            titles.append(title)
    return titles


def patch_node(
    session: requests.Session,
    base_url: str,
    content_type: str,
    uuid: str,
    title: str,
) -> bool:
    """PATCH field_business_criticality_level to 'critical'."""
    url = f"{base_url}/jsonapi/node/{content_type}/{uuid}"
    payload = {
        "data": {
            "type": f"node--{content_type}",
            "id": uuid,
            "attributes": {
                "field_business_criticality_level": PATCH_VALUE,
            },
        }
    }
    try:
        response = session.patch(url, json=payload, timeout=30)
        response.raise_for_status()
        return True
    except requests.exceptions.HTTPError as exc:
        logger.error(
            "HTTP %s patching '%s': %s",
            exc.response.status_code,
            title,
            exc.response.text[:400],
        )
    except requests.exceptions.RequestException as exc:
        logger.error("Request error patching '%s': %s", title, exc)
    return False


def main() -> None:
    args = parse_args()

    load_dotenv()
    required = [
        "DRUPAL_BASE_URL",
        "DRUPAL_USERNAME",
        "DRUPAL_PASSWORD",
        "DRUPAL_CONTENT_TYPE",
    ]
    missing = [k for k in required if not os.environ.get(k)]
    if missing:
        logger.error(
            "Missing required environment variables: %s", ", ".join(missing)
        )
        sys.exit(1)

    base_url = os.environ["DRUPAL_BASE_URL"].rstrip("/")
    username = os.environ["DRUPAL_USERNAME"]
    password = os.environ["DRUPAL_PASSWORD"]
    content_type = os.environ["DRUPAL_CONTENT_TYPE"]

    session = requests.Session()
    session.auth = (username, password)
    session.headers.update({
        "Accept": "application/vnd.api+json",
        "Content-Type": "application/vnd.api+json",
    })
    session.verify = False

    logger.info("Reading %s...", CSV_FILE)
    titles = read_csv_titles(CSV_FILE)
    logger.info("Found %d nodes to patch with '%s'", len(titles), PATCH_VALUE)

    if args["dry_run"]:
        logger.info("DRY RUN — no nodes will be patched")
        for title in titles:
            logger.info(
                "  Would set field_business_criticality_level='%s' on '%s'",
                PATCH_VALUE,
                title,
            )
        return

    logger.info("Fetching all existing nodes from Drupal...")
    node_index = fetch_all_nodes(session, base_url, content_type)
    logger.info("Indexed %d nodes", len(node_index))

    total = len(titles)
    patched = 0
    not_found = 0
    failed = 0
    start_time = time.monotonic()

    for i, title in enumerate(titles, 1):
        uuid = node_index.get(title)
        if not uuid:
            logger.warning(
                "[%d/%d] Node not found in Drupal: '%s'", i, total, title
            )
            not_found += 1
            continue

        logger.info(
            "[%d/%d] Patching '%s' → field_business_criticality_level='%s'",
            i, total, title, PATCH_VALUE,
        )
        if patch_node(session, base_url, content_type, uuid, title):
            patched += 1
        else:
            failed += 1

    elapsed = time.monotonic() - start_time
    logger.info("=" * 60)
    logger.info("PATCH CRITICALITY SUMMARY")
    logger.info("  Total entries    : %d", total)
    logger.info("  Patched          : %d", patched)
    logger.info("  Not found        : %d", not_found)
    logger.info("  Failed           : %d", failed)
    logger.info("  Elapsed          : %.1fs", elapsed)


if __name__ == "__main__":
    main()
