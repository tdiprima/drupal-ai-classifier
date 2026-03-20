#!/usr/bin/env python3
"""
probe_field_values.py

Tries candidate values for each list field until one is accepted by Drupal.
Any nodes that get created (HTTP 201) are immediately deleted.

Usage:
    python probe_field_values.py
    python probe_field_values.py field_business_criticality_level
"""

import json
import os
import sys

import requests
import urllib3
from dotenv import load_dotenv

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

CANDIDATES: dict[str, list] = {
    "field_business_criticality_level": [
        "Mission Critical", "mission_critical", "mission critical",
        "Business Essential", "business_essential", "business essential",
        "Business Core", "business_core", "business core",
        "Business Supporting", "business_supporting", "business supporting",
        "Core Infrastructure", "core_infrastructure",
        "Critical", "critical",
        "High", "high",
        "Medium", "medium",
        "Low", "low",
    ],
    "field_contains_phi": [
        "Yes", "No", "yes", "no", "YES", "NO", "1", "0", "true", "false",
    ],
    "field_mission_critical": [
        "Yes", "No", "yes", "no", "YES", "NO", "1", "0", "true", "false",
    ],
    "field_ai_application": [
        "Yes", "No", "yes", "no", "YES", "NO", "1", "0", "true", "false",
    ],
    "field_confidence": [
        "High", "Medium", "Low", "high", "medium", "low",
        "HIGH", "MEDIUM", "LOW",
    ],
    "field_status": [
        "Active", "active", "ACTIVE",
        "Inactive", "inactive", "INACTIVE",
        "Restricted", "restricted", "RESTRICTED",
        "Protected", "protected", "PROTECTED",
        "Confidential", "confidential", "CONFIDENTIAL",
        "Public", "public", "PUBLIC",
        "Retired", "retired", "RETIRED",
        "1", "0",
    ],
    "field_division": [
        "SBUH", "SBSH", "SBELIH", "CPMP", "SBAS", "HSC", "MHL", "SDM",
        "sbuh", "sbsh", "sbelih", "cpmp", "sbas", "hsc", "mhl", "sdm",
    ],
    "field_sites_used": [
        "SBUH", "SBSH", "SBELIH", "CPMP", "SBAS", "HSC", "MHL", "SDM",
        "sbuh", "sbsh", "sbelih", "cpmp", "sbas", "hsc", "mhl", "sdm",
    ],
}

# Use a unique suffix so probe nodes are easy to find and delete
PROBE_TITLE_PREFIX = "PROBE_DELETE_ME"


def field_value_is_rejected(errors: list, field_name: str) -> bool:
    """Return True if the field itself was rejected (not a valid choice)."""
    for err in errors:
        pointer = err.get("source", {}).get("pointer", "")
        detail = err.get("detail", "")
        if field_name in pointer and "not a valid choice" in detail:
            return True
    return False


def delete_node(session, base_url: str, node_id: str) -> None:
    url = f"{base_url}/jsonapi/node/{node_id}"
    session.delete(url, timeout=10)


def probe_field(
    session,
    base_url: str,
    content_type: str,
    field_name: str,
    candidates: list,
) -> None:
    url = f"{base_url}/jsonapi/node/{content_type}"
    valid_keys = []

    for candidate in candidates:
        value = [{"value": candidate}] if field_name == "field_sites_used" else candidate

        payload = {
            "data": {
                "type": f"node--{content_type}",
                "attributes": {
                    "title": f"{PROBE_TITLE_PREFIX}_{field_name}",
                    "field_vendor_name": "PROBE_VENDOR",
                    "field_product_name": "PROBE_PRODUCT",
                    "field_description": "PROBE_DESCRIPTION",
                    field_name: value,
                },
            }
        }

        response = session.post(url, json=payload, timeout=15)

        if response.status_code == 201:
            valid_keys.append(candidate)
            node_id = response.json().get("data", {}).get("id")
            if node_id:
                delete_node(session, base_url, node_id)
            continue

        if response.status_code == 422:
            try:
                errors = response.json().get("errors", [])
            except (json.JSONDecodeError, ValueError):
                continue
            # If the field itself is not in the errors, the value was accepted
            if not field_value_is_rejected(errors, field_name):
                valid_keys.append(candidate)

    print(f"\n  {field_name}:")
    if valid_keys:
        print(f"    VALID keys: {valid_keys}")
    else:
        print("    No valid keys found from candidates list")


def main() -> None:
    load_dotenv()
    base_url = os.environ.get("DRUPAL_BASE_URL", "").rstrip("/")
    username = os.environ.get("DRUPAL_USERNAME", "")
    password = os.environ.get("DRUPAL_PASSWORD", "")
    content_type = os.environ.get("DRUPAL_CONTENT_TYPE", "")

    if not all([base_url, username, password, content_type]):
        print("ERROR: missing required .env variables", file=sys.stderr)
        sys.exit(1)

    fields = sys.argv[1:] if len(sys.argv) > 1 else list(CANDIDATES.keys())
    unknown = [f for f in fields if f not in CANDIDATES]
    if unknown:
        print(f"ERROR: no candidates defined for: {unknown}", file=sys.stderr)
        sys.exit(1)

    session = requests.Session()
    session.auth = (username, password)
    session.headers.update({
        "Accept": "application/vnd.api+json",
        "Content-Type": "application/vnd.api+json",
    })
    session.verify = False

    print(f"Probing {len(fields)} field(s) on node/{content_type}...\n")
    print("(Any probe nodes created will be auto-deleted)\n")
    for field_name in fields:
        probe_field(session, base_url, content_type, field_name, CANDIDATES[field_name])

    print("\nDone. Verify no leftover probe nodes at:")
    print(f"  {base_url}/admin/content?title={PROBE_TITLE_PREFIX}")


if __name__ == "__main__":
    main()
