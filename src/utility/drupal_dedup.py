#!/usr/bin/env python3
"""
drupal_dedup.py

Finds duplicate new_product nodes in Drupal (same title) and removes
the one with fewer filled fields, keeping the more complete node.

Usage:
    python drupal_dedup.py --dry-run       # preview duplicates without deleting
    python drupal_dedup.py                 # delete duplicates (keeps fuller node)
    python drupal_dedup.py --help

Required .env variables: same as drupal_importer.py
"""

import logging
import os
import sys
import time

import requests
import urllib3
from dotenv import load_dotenv

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

PAGE_SIZE = 50

# Fields to check when deciding which node is "more complete"
CONTENT_FIELDS = [
    "field_vendor_name",
    "field_product_name",
    "field_description",
    "field_division",
    "field_status",
    "field_contains_phi",
    "field_mission_critical",
    "field_priority_for_business_cont",
    "field_business_criticality_level",
    "field_contract_terms",
    "field_certificate_expiration_dat",
    "field_responsible_dept_category",
    "field_business_sponsor_name_phon",
    "field_it_director_manager",
    "field_it_technical_contact",
    "field_sites_used",
    "field_ai_application",
    "field_confidence",
    "field_reason",
    "field_risk_review_required",
]


def parse_args() -> dict:
    """Parse command-line arguments from sys.argv."""
    args = sys.argv[1:]

    if "--help" in args or "-h" in args:
        print(__doc__)
        sys.exit(0)

    return {"dry_run": "--dry-run" in args}


def field_is_filled(value) -> bool:
    """Return True if a field value is non-empty."""
    if value is None:
        return False
    if isinstance(value, str) and value.strip() == "":
        return False
    if isinstance(value, list) and len(value) == 0:
        return False
    return True


def count_filled_fields(attributes: dict) -> int:
    """Count how many content fields have non-empty values."""
    count = 0
    for field in CONTENT_FIELDS:
        value = attributes.get(field)
        if field_is_filled(value):
            count += 1
    return count


def fetch_all_nodes_full(
    session: requests.Session, base_url: str, content_type: str
) -> list[dict]:
    """
    Fetch all nodes with all fields. Returns a list of raw JSON:API
    resource objects (each has 'id', 'attributes', etc.).
    """
    nodes = []
    url = f"{base_url}/jsonapi/node/{content_type}"
    params = {"page[limit]": PAGE_SIZE}

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
        nodes.extend(data.get("data", []))

        logger.info("  Fetched page %d (%d nodes so far)", page, len(nodes))

        url = data.get("links", {}).get("next", {}).get("href")
        params = None

    return nodes


def find_duplicates(nodes: list[dict]) -> dict[str, list[dict]]:
    """
    Group nodes by title and return only groups with more than one node.
    """
    by_title: dict[str, list[dict]] = {}
    for node in nodes:
        title = node.get("attributes", {}).get("title", "")
        if title:
            by_title.setdefault(title, []).append(node)

    return {title: group for title, group in by_title.items() if len(group) > 1}


def pick_node_to_keep(group: list[dict]) -> tuple[dict, list[dict]]:
    """
    Given a list of duplicate nodes, pick the one with the most filled
    fields to keep. Returns (keeper, list_of_nodes_to_delete).
    """
    scored = []
    for node in group:
        attrs = node.get("attributes", {})
        filled = count_filled_fields(attrs)
        scored.append((filled, node))

    # Sort descending by filled count — first one is the keeper
    scored.sort(key=lambda pair: pair[0], reverse=True)

    keeper = scored[0][1]
    to_delete = [pair[1] for pair in scored[1:]]
    return keeper, to_delete


def delete_node(
    session: requests.Session,
    base_url: str,
    content_type: str,
    uuid: str,
    title: str,
) -> bool:
    """DELETE a single node. Returns True on success."""
    url = f"{base_url}/jsonapi/node/{content_type}/{uuid}"
    try:
        response = session.delete(url, timeout=30)
        if response.status_code in (200, 204):
            return True
        logger.error(
            "HTTP %s deleting '%s' (%s): %s",
            response.status_code,
            title,
            uuid,
            response.text[:400],
        )
    except requests.exceptions.RequestException as exc:
        logger.error("Request error deleting '%s' (%s): %s", title, uuid, exc)
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

    logger.info(
        "Fetching all %s nodes from Drupal (this may take a moment)...",
        content_type,
    )
    nodes = fetch_all_nodes_full(session, base_url, content_type)
    logger.info("Fetched %d total nodes", len(nodes))

    duplicates = find_duplicates(nodes)
    if not duplicates:
        logger.info("No duplicates found — nothing to do.")
        return

    logger.info("Found %d titles with duplicates", len(duplicates))

    if args["dry_run"]:
        logger.info("DRY RUN — no nodes will be deleted")

    deleted = 0
    failed = 0
    start_time = time.monotonic()

    for title, group in sorted(duplicates.items()):
        keeper, to_delete = pick_node_to_keep(group)
        keeper_attrs = keeper.get("attributes", {})
        keeper_filled = count_filled_fields(keeper_attrs)

        logger.info("  '%s' — %d copies found", title, len(group))
        logger.info(
            "    KEEP   %s (%d fields filled)", keeper["id"], keeper_filled
        )

        for node in to_delete:
            node_attrs = node.get("attributes", {})
            node_filled = count_filled_fields(node_attrs)
            logger.info(
                "    DELETE %s (%d fields filled)", node["id"], node_filled
            )

            if not args["dry_run"]:
                if delete_node(
                    session, base_url, content_type, node["id"], title
                ):
                    deleted += 1
                else:
                    failed += 1

    elapsed = time.monotonic() - start_time
    logger.info("=" * 60)
    logger.info("DEDUP SUMMARY")
    logger.info("  Duplicate titles : %d", len(duplicates))
    if args["dry_run"]:
        total_to_delete = sum(
            len(group) - 1 for group in duplicates.values()
        )
        logger.info("  Would delete     : %d nodes", total_to_delete)
    else:
        logger.info("  Deleted          : %d", deleted)
        logger.info("  Failed           : %d", failed)
    logger.info("  Elapsed          : %.1fs", elapsed)


if __name__ == "__main__":
    main()
