#!/usr/bin/env python3
import logging
import os
import sys
from pathlib import Path

import requests
import urllib3
from dotenv import load_dotenv

load_dotenv()

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)

PAGE_SIZE = 50
AI_FIELDS = [
    "field_ai_application",
    "field_ai_notes",
    "field_risk_review_required",
    "field_reason",
]


def has_value(val) -> bool:
    if val is None:
        return False
    if isinstance(val, str):
        return val.strip() != ""
    if isinstance(val, list):
        return len(val) > 0
    return bool(val)


def fetch_nodes(session: requests.Session, base_url: str, content_type: str) -> list[dict]:
    fields_param = "id,title," + ",".join(AI_FIELDS)
    url = f"{base_url}/jsonapi/node/{content_type}"
    params = {
        f"fields[node--{content_type}]": fields_param,
        "page[limit]": PAGE_SIZE,
    }
    nodes = []
    page = 0
    while url:
        page += 1
        resp = session.get(url, params=params if page == 1 else None, timeout=30)
        if resp.status_code != 200:
            logger.error("HTTP %s fetching nodes", resp.status_code)
            sys.exit(1)
        body = resp.json()
        for node in body.get("data", []):
            attrs = node.get("attributes", {})
            nodes.append({
                "uuid": node.get("id"),
                "title": attrs.get("title", "(no title)"),
                "fields": {f: attrs.get(f) for f in AI_FIELDS},
            })
        url = body.get("links", {}).get("next", {}).get("href")
    return nodes


def main() -> None:
    session = requests.Session()
    session.auth = (os.environ["DRUPAL_USERNAME"], os.environ["DRUPAL_PASSWORD"])
    session.headers["Accept"] = "application/vnd.api+json"
    session.verify = False

    logger.info("Fetching nodes from Drupal...")
    nodes = fetch_nodes(session, os.environ["DRUPAL_BASE_URL"], os.environ["DRUPAL_CONTENT_TYPE"])
    logger.info("Total nodes fetched: %d", len(nodes))

    hits = []
    for node in nodes:
        populated = {}
        for field in AI_FIELDS:
            value = node["fields"][field]
            if has_value(value):
                populated[field] = value
        if populated:
            hits.append({"title": node["title"], "uuid": node["uuid"], "populated": populated})

    if not hits:
        logger.info("No nodes have any of the AI fields populated.")
        return

    logger.info("%d node(s) with AI fields populated:", len(hits))
    for hit in hits:
        logger.info("  [%s] %s", hit["uuid"], hit["title"])
        for field, value in hit["populated"].items():
            logger.info("    %s = %r", field, value)


if __name__ == "__main__":
    main()
