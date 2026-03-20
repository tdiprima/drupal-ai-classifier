#!/usr/bin/env python3
"""
count_nodes.py

Count nodes of a given content type by paging through JSON:API results.
Requests only node IDs (no attributes) for speed.

Usage:
    python count_nodes.py                            # default content type
    python count_nodes.py --type new_product         # explicit type
    python count_nodes.py --type article --help

Required .env variables:
    DRUPAL_BASE_URL   e.g. http://bmi-capella.uhmc.sunysb.edu
    DRUPAL_USERNAME   Drupal account with read access
    DRUPAL_PASSWORD   account password
"""

import argparse
import logging
import os
import sys

import requests
import urllib3
from dotenv import load_dotenv

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

DEFAULT_CONTENT_TYPE = "new_product"
PAGE_SIZE = 50  # request only IDs, so large pages are cheap


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--type",
        default=os.environ.get("DRUPAL_CONTENT_TYPE", DEFAULT_CONTENT_TYPE),
        help=f"Drupal content type machine name (default: {DEFAULT_CONTENT_TYPE})",
    )
    return parser.parse_args()


def load_config() -> dict:
    """Load and validate required environment variables."""
    load_dotenv()
    config = {
        "base_url": os.environ.get("DRUPAL_BASE_URL", "").rstrip("/"),
        "username": os.environ.get("DRUPAL_USERNAME", ""),
        "password": os.environ.get("DRUPAL_PASSWORD", ""),
    }
    missing = [key for key, val in config.items() if not val]
    if missing:
        logger.error("Missing required .env variables: %s", ", ".join(missing))
        sys.exit(1)
    return config


def count_nodes(base_url: str, username: str, password: str, content_type: str) -> int:
    """Page through JSON:API and count all nodes of the given content type."""
    url = f"{base_url}/jsonapi/node/{content_type}"
    params = {
        "fields[node--{content_type}]": "id",  # fetch IDs only — no attributes
        "page[limit]": PAGE_SIZE,
    }
    auth = (username, password)
    headers = {"Accept": "application/vnd.api+json"}

    total = 0
    page_num = 0

    while url:
        page_num += 1
        logger.info("Fetching page %d ...", page_num)

        try:
            response = requests.get(
                url,
                auth=auth,
                headers=headers,
                params=params if page_num == 1 else None,
                verify=False,
                timeout=30,
            )
        except requests.RequestException as exc:
            logger.error("Request failed on page %d: %s", page_num, exc)
            sys.exit(1)

        if response.status_code != 200:
            logger.error("HTTP %d on page %d: %s", response.status_code, page_num, response.text[:400])
            sys.exit(1)

        payload = response.json()
        data = payload.get("data", [])
        total += len(data)

        # Follow the next-page link if present; params are embedded in the URL
        url = payload.get("links", {}).get("next", {}).get("href")

    return total


def main() -> None:
    """Entry point."""
    args = parse_args()
    config = load_config()

    content_type = args.type
    logger.info("Counting nodes of type: %s", content_type)

    count = count_nodes(
        base_url=config["base_url"],
        username=config["username"],
        password=config["password"],
        content_type=content_type,
    )

    print(f"\nTotal {content_type} nodes: {count}")


if __name__ == "__main__":
    main()
