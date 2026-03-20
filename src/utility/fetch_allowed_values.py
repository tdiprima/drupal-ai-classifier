#!/usr/bin/env python3
"""
fetch_allowed_values.py

Fetches one existing node of the configured content type and prints
the values stored in list fields so you can see the exact keys Drupal uses.

Usage:
    python fetch_allowed_values.py
"""

import json
import os
import sys

import requests
import urllib3
from dotenv import load_dotenv

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

FIELDS_OF_INTEREST = [
    "field_business_criticality_level",
    "field_contains_phi",
    "field_mission_critical",
    "field_status",
    "field_division",
    "field_priority_for_business_cont",
    "field_ai_application",
    "field_confidence",
]


def main() -> None:
    load_dotenv()
    base_url = os.environ.get("DRUPAL_BASE_URL", "").rstrip("/")
    username = os.environ.get("DRUPAL_USERNAME", "")
    password = os.environ.get("DRUPAL_PASSWORD", "")
    content_type = os.environ.get("DRUPAL_CONTENT_TYPE", "")

    if not all([base_url, username, password, content_type]):
        print("ERROR: missing required .env variables", file=sys.stderr)
        sys.exit(1)

    url = f"{base_url}/jsonapi/node/{content_type}"
    params = {"page[limit]": 3}

    response = requests.get(
        url,
        auth=(username, password),
        headers={"Accept": "application/vnd.api+json"},
        params=params,
        verify=False,
        timeout=15,
    )

    if response.status_code != 200:
        print(f"ERROR: HTTP {response.status_code}")
        print(response.text[:600])
        sys.exit(1)

    data = response.json().get("data", [])
    if not data:
        print("No existing nodes found — import something first, then re-run.")
        sys.exit(0)

    print(f"Found {len(data)} node(s). Showing list field values:\n")
    for node in data:
        title = node.get("attributes", {}).get("title", "(no title)")
        print(f"  Node: {title}")
        attrs = node.get("attributes", {})
        for field in FIELDS_OF_INTEREST:
            if field in attrs:
                print(f"    {field}: {json.dumps(attrs[field])}")
        print()


if __name__ == "__main__":
    main()
