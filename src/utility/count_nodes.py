"""
count_nodes.py — Count nodes of a given content type in Drupal via JSON:API.

Usage:
    python count_nodes.py [--type <content_type>]

Environment variables:
    DRUPAL_BASE_URL   Base URL of the Drupal instance (required)
    DRUPAL_USERNAME   Drupal account username (required)
    DRUPAL_PASSWORD   Drupal account password (required)

Defaults to content type 'new_product' if --type is not provided.
"""

import argparse
import logging
import os
import sys
import urllib3

import requests
from dotenv import load_dotenv

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)


def load_config() -> dict:
    """Load and validate required environment variables."""
    load_dotenv()
    required = ["DRUPAL_BASE_URL", "DRUPAL_USERNAME", "DRUPAL_PASSWORD"]
    missing = [key for key in required if not os.environ.get(key)]
    if missing:
        logger.error("Missing required environment variables: %s", ", ".join(missing))
        sys.exit(1)
    return {
        "base_url": os.environ["DRUPAL_BASE_URL"].rstrip("/"),
        "username": os.environ["DRUPAL_USERNAME"],
        "password": os.environ["DRUPAL_PASSWORD"],
    }


def build_session(username: str, password: str) -> requests.Session:
    """Create an authenticated requests session for the Drupal JSON:API."""
    session = requests.Session()
    session.auth = (username, password)
    session.headers.update({"Accept": "application/vnd.api+json"})
    # NOTE: verify=False is acceptable for internal university servers.
    session.verify = False
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    return session


def count_nodes(session: requests.Session, base_url: str, content_type: str) -> int:
    """
    Return the total number of nodes of the given content type.

    Uses the JSON:API meta.count field from the first page to get the total
    without fetching all records.
    """
    url = f"{base_url}/jsonapi/node/{content_type}"
    params = {
        "page[limit]": 1,          # we only need the count, not the data
        "page[offset]": 0,
    }

    try:
        response = session.get(url, params=params, timeout=30)
        response.raise_for_status()
    except requests.exceptions.HTTPError as exc:
        logger.error("HTTP error fetching nodes: %s", exc)
        sys.exit(1)
    except requests.exceptions.ConnectionError as exc:
        logger.error("Connection error: %s", exc)
        sys.exit(1)
    except requests.exceptions.Timeout:
        logger.error("Request timed out")
        sys.exit(1)

    body = response.json()
    count = body.get("meta", {}).get("count")
    if count is None:
        logger.error(
            "Response did not include meta.count. "
            "Ensure the JSON:API count module is enabled on the Drupal site."
        )
        sys.exit(1)

    return int(count)


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="Count Drupal nodes of a given content type via JSON:API."
    )
    parser.add_argument(
        "--type",
        dest="content_type",
        default="new_product",
        help="Drupal machine name of the content type (default: new_product)",
    )
    return parser.parse_args()


def main() -> None:
    """Entry point."""
    args = parse_args()
    config = load_config()
    session = build_session(config["username"], config["password"])

    logger.info(
        "Counting nodes of type '%s' at %s",
        args.content_type,
        config["base_url"],
    )

    total = count_nodes(session, config["base_url"], args.content_type)
    print(f"Total '{args.content_type}' nodes: {total}")


if __name__ == "__main__":
    main()
