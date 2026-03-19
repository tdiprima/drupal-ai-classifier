#!/usr/bin/env python3
"""
fetch_field_config.py

Fetches the allowed values (key → label) for list fields on the configured
content type directly from the Drupal field config API.

Usage:
    python fetch_field_config.py
    python fetch_field_config.py field_business_criticality_level field_status
"""

import json
import os
import sys
import urllib3

import requests
from dotenv import load_dotenv

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

FIELDS_OF_INTEREST = [
    "field_business_criticality_level",
    "field_contains_phi",
    "field_mission_critical",
    "field_status",
    "field_division",
    "field_sites_used",
    "field_ai_application",
    "field_confidence",
]


def fetch_field_storage_config(session, base_url: str, field_name: str) -> dict | None:
    """Fetch field storage config for a single field via JSON:API."""
    url = f"{base_url}/jsonapi/field_storage_config/field_storage_config/node.{field_name}"
    response = session.get(url, timeout=15)
    if response.status_code == 200:
        return response.json().get("data", {})
    return None


def fetch_field_config(session, base_url: str, content_type: str, field_name: str) -> dict | None:
    """Fetch field instance config (includes allowed values) via JSON:API."""
    url = f"{base_url}/jsonapi/field_config/field_config/node.{content_type}.{field_name}"
    response = session.get(url, timeout=15)
    if response.status_code == 200:
        return response.json().get("data", {})
    return None


def print_allowed_values(field_name: str, config_data: dict) -> None:
    settings = config_data.get("attributes", {}).get("settings", {})
    allowed = settings.get("allowed_values", [])
    if not allowed:
        # Also check field storage settings
        storage_settings = config_data.get("attributes", {}).get("settings", {})
        allowed = storage_settings.get("allowed_values", [])

    if allowed:
        print(f"\n  {field_name}:")
        for entry in allowed:
            key = entry.get("value", entry.get("key", "?"))
            label = entry.get("label", key)
            print(f"    key={key!r:30s} label={label!r}")
    else:
        print(f"\n  {field_name}: (no allowed_values found in config)")
        print(f"    raw settings: {json.dumps(settings, indent=6)}")


def main() -> None:
    load_dotenv()
    base_url = os.environ.get("DRUPAL_BASE_URL", "").rstrip("/")
    username = os.environ.get("DRUPAL_USERNAME", "")
    password = os.environ.get("DRUPAL_PASSWORD", "")
    content_type = os.environ.get("DRUPAL_CONTENT_TYPE", "")

    if not all([base_url, username, password, content_type]):
        print("ERROR: missing required .env variables", file=sys.stderr)
        sys.exit(1)

    fields = sys.argv[1:] if len(sys.argv) > 1 else FIELDS_OF_INTEREST

    session = requests.Session()
    session.auth = (username, password)
    session.headers.update({"Accept": "application/vnd.api+json"})
    session.verify = False

    print("Fetching field configs from Drupal...\n")
    for field_name in fields:
        # Try field instance config first (has allowed_values for list fields)
        data = fetch_field_config(session, base_url, content_type, field_name)
        if data:
            print_allowed_values(field_name, data)
            continue

        # Fall back to field storage config
        data = fetch_field_storage_config(session, base_url, field_name)
        if data:
            print_allowed_values(field_name, data)
            continue

        print(f"\n  {field_name}: could not fetch (403 or not found)")


if __name__ == "__main__":
    main()
