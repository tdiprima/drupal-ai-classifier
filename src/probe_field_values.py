#!/usr/bin/env python3
"""
probe_field_values.py

POSTs a minimal test node with a sentinel value for one field at a time.
Drupal's 422 error often lists the accepted values, telling us the real keys.

The test node title starts with 'PROBE_DELETE_' so it's easy to find and
delete from the Drupal admin UI after you're done.

Usage:
    python probe_field_values.py                        # probes all list fields
    python probe_field_values.py field_business_criticality_level
"""

import json
import os
import sys
import urllib3

import requests
from dotenv import load_dotenv

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

PROBE_FIELDS = [
    "field_business_criticality_level",
    "field_contains_phi",
    "field_mission_critical",
    "field_status",
    "field_division",
    "field_sites_used",
    "field_ai_application",
    "field_confidence",
]

SENTINEL = "__PROBE__"


def probe_field(session, base_url: str, content_type: str, field_name: str) -> None:
    """POST a node with a sentinel value for field_name and print the full error."""
    url = f"{base_url}/jsonapi/node/{content_type}"

    if field_name == "field_sites_used":
        value = [{"value": SENTINEL}]
    else:
        value = SENTINEL

    payload = {
        "data": {
            "type": f"node--{content_type}",
            "attributes": {
                "title": f"PROBE_DELETE_{field_name}",
                field_name: value,
            },
        }
    }

    response = session.post(url, json=payload, timeout=15)

    print(f"\n{'=' * 60}")
    print(f"Field: {field_name}  →  HTTP {response.status_code}")

    if response.status_code == 201:
        node_id = response.json().get("data", {}).get("id", "?")
        print(f"  WARNING: probe node was CREATED (id={node_id}) — delete it from Drupal admin")
        return

    try:
        body = response.json()
        errors = body.get("errors", [])
        for err in errors:
            print(f"  detail : {err.get('detail', '')}")
            print(f"  pointer: {err.get('source', {}).get('pointer', '')}")
    except (json.JSONDecodeError, ValueError):
        print(f"  raw response: {response.text[:600]}")


def main() -> None:
    load_dotenv()
    base_url = os.environ.get("DRUPAL_BASE_URL", "").rstrip("/")
    username = os.environ.get("DRUPAL_USERNAME", "")
    password = os.environ.get("DRUPAL_PASSWORD", "")
    content_type = os.environ.get("DRUPAL_CONTENT_TYPE", "")

    if not all([base_url, username, password, content_type]):
        print("ERROR: missing required .env variables", file=sys.stderr)
        sys.exit(1)

    fields = sys.argv[1:] if len(sys.argv) > 1 else PROBE_FIELDS

    session = requests.Session()
    session.auth = (username, password)
    session.headers.update({
        "Accept": "application/vnd.api+json",
        "Content-Type": "application/vnd.api+json",
    })
    session.verify = False

    print(f"Probing {len(fields)} field(s) on node/{content_type}...")
    for field_name in fields:
        probe_field(session, base_url, content_type, field_name)

    print(f"\n{'=' * 60}")
    print("Done. If any probe nodes were created, delete them from:")
    print(f"  {base_url}/admin/content?title=PROBE_DELETE")


if __name__ == "__main__":
    main()
