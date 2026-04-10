#!/usr/bin/env python3
"""
Replace double dashes (--) with a single dash (-) in Drupal node titles.

Usage:
    # Dry run (default) - shows what would change, changes nothing
    python normalize_title_dashes.py

    # Actually write changes
    python normalize_title_dashes.py --apply

Reads DRUPAL_BASE_URL, DRUPAL_USERNAME, DRUPAL_PASSWORD, DRUPAL_CONTENT_TYPE
from the .env file (same as the rest of this project).
"""

import argparse
import logging
import re

import requests

from drupal_importer import load_config
from normalize_smart_quotes import DrupalClient

logger = logging.getLogger(__name__)

# Match two or more consecutive dashes so we collapse "---" to "-" as well.
_MULTI_DASH = re.compile(r"-{2,}")


def fix_dashes(title: str) -> str:
    """Replace any run of two or more dashes with a single dash."""
    return _MULTI_DASH.sub("-", title)


def needs_fix(title: str) -> bool:
    return "--" in title


def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true",
                    help="Actually write changes. Default is dry-run.")
    args = ap.parse_args()

    config = load_config()
    client = DrupalClient(config["base_url"], config["username"], config["password"])

    # Collect ALL nodes before patching anything.
    # Patching changes Drupal's `changed` timestamp, which shifts sort order
    # mid-pagination and causes nodes to be skipped if we patch while iterating.
    logger.info("Fetching all nodes...")
    all_nodes = list(client.list_nodes(config["content_type"]))
    scanned = len(all_nodes)
    logger.info("Fetched %d node(s)", scanned)

    pending = []
    for node in all_nodes:
        title = node.get("attributes", {}).get("title", "")
        if needs_fix(title):
            pending.append(node)

    to_fix = len(pending)
    fixed = 0
    errors = 0

    for node in pending:
        uuid = node["id"]
        attrs = node.get("attributes", {})
        nid = attrs.get("drupal_internal__nid", "?")
        title = attrs.get("title", "")
        new_title = fix_dashes(title)
        logger.info("[nid=%s] %r  ->  %r", nid, title, new_title)

        if args.apply:
            try:
                client.patch_node(config["content_type"], uuid, {"title": new_title})
                fixed += 1
            except requests.exceptions.HTTPError as exc:
                errors += 1
                logger.error("PATCH failed for nid=%s: %s — %s", nid, exc, exc.response.text[:300])

    logger.info("")
    logger.info("Scanned : %d", scanned)
    logger.info("To fix  : %d", to_fix)
    if args.apply:
        logger.info("Fixed   : %d", fixed)
        if errors:
            logger.warning("Errors  : %d", errors)
    else:
        logger.info("(dry run — re-run with --apply to write changes)")


if __name__ == "__main__":
    main()
