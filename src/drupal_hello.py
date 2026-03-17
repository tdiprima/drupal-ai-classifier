#!/usr/bin/env python3
"""
drupal_hello.py

Hello-world connectivity test for the Drupal JSON:API.
Loads credentials from a .env file, connects to Drupal,
and prints the site name plus the first few software nodes it finds.

Usage:
    python drupal_hello.py

Required .env variables:
    DRUPAL_BASE_URL       e.g. https://example.com
    DRUPAL_USERNAME       dedicated scanner account
    DRUPAL_PASSWORD       scanner account password
    DRUPAL_CONTENT_TYPE   machine name of the content type, e.g. software_entry
"""

import logging
import os
import sys

import requests
from dotenv import load_dotenv

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


def load_config() -> dict:
    """Load and validate required environment variables."""
    load_dotenv()

    required = ["DRUPAL_BASE_URL", "DRUPAL_USERNAME", "DRUPAL_PASSWORD", "DRUPAL_CONTENT_TYPE"]
    missing = [key for key in required if not os.environ.get(key)]
    if missing:
        logger.error("Missing required environment variables: %s", ", ".join(missing))
        sys.exit(1)

    return {
        "base_url": os.environ["DRUPAL_BASE_URL"].rstrip("/"),
        "username": os.environ["DRUPAL_USERNAME"],
        "password": os.environ["DRUPAL_PASSWORD"],
        "content_type": os.environ["DRUPAL_CONTENT_TYPE"],
    }


def fetch_site_name(base_url: str, session: requests.Session) -> str:
    """Fetch the Drupal site name via JSON:API."""
    url = f"{base_url}/jsonapi"
    response = session.get(url, timeout=10)
    response.raise_for_status()
    return response.json().get("meta", {}).get("links", {}).get("me", {}).get("meta", {}).get("body", "unknown")


def fetch_first_nodes(base_url: str, content_type: str, session: requests.Session, limit: int = 3) -> list[dict]:
    """Fetch the first N nodes of the given content type via JSON:API."""
    url = f"{base_url}/jsonapi/node/{content_type}"
    params = {"page[limit]": limit}
    response = session.get(url, params=params, timeout=10)
    response.raise_for_status()
    return response.json().get("data", [])


def print_node_summary(nodes: list[dict]) -> None:
    """Print a short summary of each node."""
    if not nodes:
        logger.info("No nodes found — the content type may be empty.")
        return

    for node in nodes:
        node_id = node.get("id", "?")
        attributes = node.get("attributes", {})
        title = attributes.get("title", "(no title)")
        status = "published" if attributes.get("status") else "unpublished"
        logger.info("  Node %s | %s | %s", node_id, status, title)


def main() -> None:
    config = load_config()
    base_url = config["base_url"]

    session = requests.Session()
    session.auth = (config["username"], config["password"])
    session.headers.update({"Accept": "application/vnd.api+json"})
    # NOTE: verify=False skips SSL cert validation — acceptable for testing on
    # internal/university servers with untrusted certs. Remove before production.
    session.verify = False
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    logger.info("Connecting to %s ...", base_url)

    try:
        nodes = fetch_first_nodes(base_url, config["content_type"], session)
    except requests.exceptions.SSLError as exc:
        logger.error("SSL error connecting to %s: %s", base_url, exc)
        sys.exit(1)
    except requests.exceptions.ConnectionError as exc:
        logger.error("Could not reach %s: %s", base_url, exc)
        sys.exit(1)
    except requests.exceptions.HTTPError as exc:
        logger.error("HTTP error: %s", exc)
        sys.exit(1)

    logger.info("Connection successful.")
    logger.info("First %d node(s) of type '%s':", len(nodes), config["content_type"])
    print_node_summary(nodes)
    logger.info("Done.")


if __name__ == "__main__":
    main()
