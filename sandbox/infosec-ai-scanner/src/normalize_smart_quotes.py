#!/usr/bin/env python3
"""
Normalize smart quotes (and a few related Unicode punctuation marks) in all
nodes of a given Drupal content type via JSON:API.

Usage:
    # Dry run (default) - shows what would change, changes nothing
    python normalize_smart_quotes.py

    # Actually write changes
    python normalize_smart_quotes.py --apply

    # Limit to specific fields instead of auto-detecting
    python normalize_smart_quotes.py --fields field_reason field_description

Requires: requests  (pip install requests)
"""

import argparse
import logging
from typing import Any, Iterable

import requests
import urllib3

from drupal_importer import load_config
from drupal_ai_scanner import normalize_text

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

logger = logging.getLogger(__name__)

# ---- Config ---------------------------------------------------------------

PAGE_SIZE = 50
TIMEOUT = 30

# ---- Helpers --------------------------------------------------------------

def needs_fix(value: Any) -> bool:
    """Return True if value contains any character that normalize_text would replace."""
    if not isinstance(value, str):
        return False
    return normalize_text(value) != value


def iter_text_fields(attributes: dict, allowlist: set[str] | None) -> Iterable[tuple[str, Any]]:
    """
    Yield (field_name, current_value) for fields that look like text.

    Drupal text fields come through JSON:API as either:
      - a plain string  (e.g. title)
      - a dict with 'value', 'format', 'processed', 'summary'  (formatted text)
    """
    for name, val in attributes.items():
        if allowlist is not None and name not in allowlist:
            continue
        if isinstance(val, str):
            yield name, val
        elif isinstance(val, dict) and "value" in val and isinstance(val["value"], str):
            yield name, val  # yield the whole dict so we can rebuild it
        # lists of either of the above (multi-value fields)
        elif isinstance(val, list):
            for item in val:
                if isinstance(item, str) or (isinstance(item, dict) and "value" in item):
                    yield name, val
                    break


def build_patched_attributes(attributes: dict, allowlist: set[str] | None) -> dict:
    """
    Return a dict of ONLY the changed attributes, in the shape JSON:API wants.
    """
    changed: dict = {}
    for name, val in iter_text_fields(attributes, allowlist):
        if isinstance(val, str):
            if needs_fix(val):
                changed[name] = normalize_text(val)
        elif isinstance(val, dict):
            if needs_fix(val.get("value")):
                new_val = dict(val)
                new_val["value"] = normalize_text(val["value"])
                # drop 'processed' - Drupal will regenerate it
                new_val.pop("processed", None)
                changed[name] = new_val
        elif isinstance(val, list):
            new_list = []
            any_change = False
            for item in val:
                if isinstance(item, str) and needs_fix(item):
                    new_list.append(normalize_text(item))
                    any_change = True
                elif isinstance(item, dict) and needs_fix(item.get("value")):
                    new_item = dict(item)
                    new_item["value"] = normalize_text(item["value"])
                    new_item.pop("processed", None)
                    new_list.append(new_item)
                    any_change = True
                else:
                    new_list.append(item)
            if any_change:
                changed[name] = new_list
    return changed


# ---- JSON:API client ------------------------------------------------------

class DrupalClient:
    def __init__(self, base_url: str, user: str, password: str):
        self.base = base_url.rstrip("/")
        self.session = requests.Session()
        self.session.auth = (user, password)
        self.session.verify = False
        self.session.headers.update({
            "Accept": "application/vnd.api+json",
            "Content-Type": "application/vnd.api+json",
        })

    def list_nodes(self, bundle: str):
        url = f"{self.base}/jsonapi/node/{bundle}?page[limit]={PAGE_SIZE}"
        while url:
            r = self.session.get(url, timeout=TIMEOUT)
            r.raise_for_status()
            payload = r.json()
            for node in payload.get("data", []):
                yield node
            url = payload.get("links", {}).get("next", {}).get("href")

    def patch_node(self, bundle: str, uuid: str, attributes: dict):
        url = f"{self.base}/jsonapi/node/{bundle}/{uuid}"
        body = {
            "data": {
                "type": f"node--{bundle}",
                "id": uuid,
                "attributes": attributes,
            }
        }
        r = self.session.patch(url, json=body, timeout=TIMEOUT)
        r.raise_for_status()
        return r.json()


# ---- Main -----------------------------------------------------------------

def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true",
                    help="Actually write changes. Default is dry-run.")
    ap.add_argument("--fields", nargs="*", default=None,
                    help="Restrict to these field machine names (e.g. field_reason).")
    args = ap.parse_args()

    config = load_config()
    client = DrupalClient(config["base_url"], config["username"], config["password"])
    allowlist = set(args.fields) if args.fields else None

    scanned = 0
    to_fix = 0
    fixed = 0
    errors = 0

    for node in client.list_nodes(config["content_type"]):
        scanned += 1
        uuid = node["id"]
        nid = node.get("attributes", {}).get("drupal_internal__nid", "?")
        title = node.get("attributes", {}).get("title", "")

        changed = build_patched_attributes(node.get("attributes", {}), allowlist)
        if not changed:
            continue

        to_fix += 1
        logger.info("[nid=%s] %r", nid, title)
        for fname in changed:
            logger.info("    - %s", fname)

        if args.apply:
            try:
                client.patch_node(config["content_type"], uuid, changed)
                fixed += 1
            except requests.exceptions.HTTPError as exc:
                errors += 1
                logger.error("PATCH failed for nid=%s: %s — %s", nid, exc, exc.response.text[:300])

    logger.info("")
    logger.info("Scanned: %d", scanned)
    logger.info("Needed fixing: %d", to_fix)
    if args.apply:
        logger.info("Successfully patched: %d", fixed)
        if errors:
            logger.warning("Errors: %d", errors)
    else:
        logger.info("(dry run — re-run with --apply to write changes)")


if __name__ == "__main__":
    main()
