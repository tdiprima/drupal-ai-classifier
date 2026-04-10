#!/usr/bin/env python3
from __future__ import annotations
"""
drupal_merge_duplicates.py

Finds Drupal nodes where title, vendor name, product name, and description
are all identical (duplicate records). For each duplicate group, merges the
unique content from all duplicates into one primary node (keeping the lowest
node ID), then deletes the secondary node(s).

Merge rules:
  - field_sites_used     : union across all duplicates
  - all other fields     : keep primary's value; fill empty slots from secondaries

Usage:
    python drupal_merge_duplicates.py --dry-run                  # preview without changing anything
    python drupal_merge_duplicates.py                            # live merge + delete
    python drupal_merge_duplicates.py --debug                    # verbose output
    python drupal_merge_duplicates.py --diff 576 607             # show why two nodes don't auto-match
    python drupal_merge_duplicates.py --force-merge 576 607      # merge 607 into 576, delete 607
    python drupal_merge_duplicates.py --dry-run --force-merge 576 607  # preview forced merge
    python drupal_merge_duplicates.py --help

Required .env variables:
    DRUPAL_BASE_URL
    DRUPAL_USERNAME
    DRUPAL_PASSWORD
    DRUPAL_CONTENT_TYPE
"""

import json
import logging
import sys
import time
from pathlib import Path

import requests
import urllib3
from dotenv import load_dotenv

from drupal_importer import load_config

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

logger = logging.getLogger(__name__)

PAGE_SIZE = 50
MAX_RETRIES = 3
RETRY_DELAY_SECONDS = 5

# All scalar fields that can be merged (first non-empty wins)
SCALAR_FIELDS = [
    "field_division",
    "field_status",
    "field_contains_phi",
    "field_mission_critical",
    "field_priority_for_business_cont",
    "field_business_criticality_level",
    "field_contract_terms",
    "field_certificate_expiration_dat",
    "field_responsible_dept_category",
    "field_business_sponsor_name_phon",
    "field_it_director_manager",
    "field_it_technical_contact",
    "field_ai_application",
    "field_confidence",
    "field_reason",
    "field_risk_review_required",
]


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

def parse_args() -> dict:
    """Parse command-line arguments from sys.argv."""
    args = sys.argv[1:]

    if "--help" in args or "-h" in args:
        print(__doc__)
        sys.exit(0)

    force_merge = None
    if "--force-merge" in args:
        idx = args.index("--force-merge")
        if idx + 2 >= len(args):
            print("ERROR: --force-merge requires two nid arguments: --force-merge <keep_nid> <discard_nid>")
            sys.exit(1)
        try:
            force_merge = (int(args[idx + 1]), int(args[idx + 2]))
        except ValueError:
            print("ERROR: --force-merge nids must be integers")
            sys.exit(1)

    diff_pair = None
    if "--diff" in args:
        idx = args.index("--diff")
        if idx + 2 >= len(args):
            print("ERROR: --diff requires two nid arguments: --diff <nid1> <nid2>")
            sys.exit(1)
        try:
            diff_pair = (int(args[idx + 1]), int(args[idx + 2]))
        except ValueError:
            print("ERROR: --diff nids must be integers")
            sys.exit(1)

    return {
        "dry_run": "--dry-run" in args,
        "debug": "--debug" in args,
        "force_merge": force_merge,
        "diff_pair": diff_pair,
    }


# ---------------------------------------------------------------------------
# Fetching nodes
# ---------------------------------------------------------------------------

def _all_fields_param(content_type: str) -> str:
    """Build the JSON:API fields selector for all relevant node attributes."""
    fields = [
        "id",
        "drupal_internal__nid",
        "title",
        "changed",
        "field_vendor_name",
        "field_product_name",
        "field_description",
        "field_sites_used",
    ] + SCALAR_FIELDS
    return ",".join(fields)


def fetch_all_nodes(
    session: requests.Session,
    base_url: str,
    content_type: str,
) -> list[dict]:
    """
    Fetch every published node of the given content type with all fields.
    Walks JSON:API pagination automatically.
    """
    nodes = []
    url = f"{base_url}/jsonapi/node/{content_type}"
    params = {
        f"fields[node--{content_type}]": _all_fields_param(content_type),
        "page[limit]": PAGE_SIZE,
    }

    page = 0
    while url:
        page += 1
        response = session.get(url, params=params if page == 1 else None, timeout=30)
        if response.status_code != 200:
            logger.error("HTTP %s fetching page %d", response.status_code, page)
            break

        body = response.json()
        for item in body.get("data", []):
            node = _parse_node(item)
            if node:
                nodes.append(node)

        url = body.get("links", {}).get("next", {}).get("href")

    # Drupal JSON:API offset pagination can return the same node on two pages
    # if a record is updated between requests. Deduplicate by UUID.
    seen: set[str] = set()
    unique_nodes = []
    for node in nodes:
        if node["uuid"] not in seen:
            seen.add(node["uuid"])
            unique_nodes.append(node)

    if len(unique_nodes) < len(nodes):
        logger.warning(
            "Dropped %d duplicate node(s) from pagination (same UUID fetched twice)",
            len(nodes) - len(unique_nodes),
        )

    logger.info("Fetched %d node(s) total", len(unique_nodes))
    return unique_nodes


def fetch_node_by_nid(
    session: requests.Session,
    base_url: str,
    content_type: str,
    nid: int,
) -> dict | None:
    """
    Fetch a single node by its integer node ID.
    Returns the parsed node dict, or None if not found.
    """
    url = f"{base_url}/jsonapi/node/{content_type}"
    params = {
        f"fields[node--{content_type}]": _all_fields_param(content_type),
        "filter[drupal_internal__nid]": nid,
    }
    try:
        response = session.get(url, params=params, timeout=30)
        response.raise_for_status()
        items = response.json().get("data", [])
        if not items:
            logger.error("Node nid=%d not found", nid)
            return None
        return _parse_node(items[0])
    except requests.exceptions.RequestException as exc:
        logger.error("Error fetching nid=%d: %s", nid, exc)
        return None


def _parse_node(item: dict) -> dict | None:
    """Extract fields from a JSON:API node item into a flat dict."""
    uuid = item.get("id", "")
    if not uuid:
        return None

    attrs = item.get("attributes", {})
    nid = attrs.get("drupal_internal__nid") or 0

    # field_sites_used is a multi-value list field
    sites_raw = attrs.get("field_sites_used") or []
    if isinstance(sites_raw, list):
        sites = {entry["value"] for entry in sites_raw if isinstance(entry, dict) and entry.get("value")}
    else:
        sites = set()

    node = {
        "uuid": uuid,
        "nid": int(nid),
        "title": attrs.get("title") or "",
        "vendor": attrs.get("field_vendor_name") or "",
        "product": attrs.get("field_product_name") or "",
        "description": _extract_text(attrs.get("field_description")),
        "field_sites_used": sites,
    }

    for field in SCALAR_FIELDS:
        raw = attrs.get(field)
        node[field] = _extract_scalar(raw)

    return node


def _extract_text(value) -> str:
    """Pull plain text from a Drupal text field (may be a dict with 'value' key)."""
    if value is None:
        return ""
    if isinstance(value, dict):
        return (value.get("value") or "").strip()
    return str(value).strip()


def _extract_scalar(value) -> str:
    """Normalize a scalar field value to a plain string."""
    if value is None:
        return ""
    if isinstance(value, dict):
        return str(value.get("value") or "").strip()
    return str(value).strip()


# ---------------------------------------------------------------------------
# Diff helper
# ---------------------------------------------------------------------------

def show_diff(keep: dict, discard: dict) -> None:
    """Log a side-by-side comparison of the four match fields for two nodes."""
    match_fields = [
        ("title",       "title"),
        ("vendor",      "field_vendor_name"),
        ("product",     "field_product_name"),
        ("description", "field_description"),
    ]
    logger.info("Field diff between nid=%d (keep) and nid=%d (discard):", keep["nid"], discard["nid"])
    any_diff = False
    for label, key in match_fields:
        keep_val = keep.get(key) or keep.get(label, "")
        discard_val = discard.get(key) or discard.get(label, "")
        if keep_val.lower().strip() != discard_val.lower().strip():
            logger.info("  %-15s KEEP    : %r", label, keep_val)
            logger.info("  %-15s DISCARD : %r", label, discard_val)
            any_diff = True
        else:
            logger.info("  %-15s (same)  : %r", label, keep_val)
    if not any_diff:
        logger.info("  All four match fields are identical — auto-detection should have caught this.")
        logger.info("  Check for hidden whitespace or Unicode differences above.")


# ---------------------------------------------------------------------------
# Duplicate detection
# ---------------------------------------------------------------------------

def _match_key(node: dict) -> tuple:
    """
    Build the identity key used to detect duplicates.
    Normalized to lowercase so casing differences don't create false negatives.
    """
    return (
        node["title"].lower().strip(),
        node["vendor"].lower().strip(),
        node["product"].lower().strip(),
        node["description"].lower().strip(),
    )


def find_duplicate_groups(nodes: list[dict]) -> list[list[dict]]:
    """
    Group nodes by (title, vendor, product, description).
    Returns only groups with 2+ members — those are the duplicates.
    """
    groups: dict[tuple, list[dict]] = {}
    for node in nodes:
        key = _match_key(node)
        # Skip nodes that are missing all four identity fields
        if not any(key):
            continue
        groups.setdefault(key, []).append(node)

    duplicates = [group for group in groups.values() if len(group) > 1]
    logger.info("Found %d duplicate group(s)", len(duplicates))
    return duplicates


# ---------------------------------------------------------------------------
# Merge logic
# ---------------------------------------------------------------------------

def choose_primary(group: list[dict]) -> tuple[dict, list[dict]]:
    """
    Select the node to keep (lowest nid = oldest) and return the rest as secondaries.
    Falls back to UUID sort if nid is unavailable.
    """
    sorted_group = sorted(group, key=lambda n: (n["nid"] or 0, n["uuid"]))
    primary = sorted_group[0]
    secondaries = sorted_group[1:]
    return primary, secondaries


def merge_into_primary(primary: dict, secondaries: list[dict]) -> dict:
    """
    Return a dict of attributes to PATCH onto the primary node.
    Only includes fields that changed (i.e. new data was found in secondaries).
    """
    changes: dict = {}

    # Union all site sets
    merged_sites = set(primary["field_sites_used"])
    for secondary in secondaries:
        merged_sites |= secondary["field_sites_used"]

    if merged_sites != primary["field_sites_used"]:
        changes["field_sites_used"] = [{"value": site} for site in sorted(merged_sites)]

    # Fill empty scalar fields from secondaries
    for field in SCALAR_FIELDS:
        if primary.get(field):
            continue  # primary already has a value — do not overwrite
        for secondary in secondaries:
            value = secondary.get(field)
            if value:
                changes[field] = value
                logger.debug("  Filling %s from secondary nid=%d: %r", field, secondary["nid"], value)
                break

    return changes


# ---------------------------------------------------------------------------
# Drupal: PATCH and DELETE
# ---------------------------------------------------------------------------

def patch_node(
    session: requests.Session,
    base_url: str,
    content_type: str,
    uuid: str,
    attributes: dict,
    title: str,
) -> bool:
    """PATCH a node with merged attributes. Returns True on success."""
    url = f"{base_url}/jsonapi/node/{content_type}/{uuid}"
    payload = {
        "data": {
            "type": f"node--{content_type}",
            "id": uuid,
            "attributes": attributes,
        }
    }
    try:
        response = session.patch(url, json=payload, timeout=30)
        response.raise_for_status()
        logger.info("  PATCHED primary '%s' (uuid=%s)", title, uuid)
        return True
    except requests.exceptions.HTTPError as exc:
        logger.error(
            "  HTTP %s patching '%s': %s",
            exc.response.status_code, title, exc.response.text[:400],
        )
    except requests.exceptions.RequestException as exc:
        logger.error("  Request error patching '%s': %s", title, exc)
    return False


def delete_node(
    session: requests.Session,
    base_url: str,
    content_type: str,
    uuid: str,
    nid: int,
    title: str,
) -> bool:
    """DELETE a secondary node. Returns True on success."""
    url = f"{base_url}/jsonapi/node/{content_type}/{uuid}"

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = session.delete(url, timeout=30)
            if response.status_code == 204:
                logger.info("  DELETED nid=%d '%s' (uuid=%s)", nid, title, uuid)
                return True
            response.raise_for_status()
        except requests.exceptions.HTTPError as exc:
            status = exc.response.status_code
            if 400 <= status < 500:
                logger.error(
                    "  HTTP %s deleting nid=%d '%s' (not retrying): %s",
                    status, nid, title, exc.response.text[:400],
                )
                return False
            logger.warning(
                "  HTTP %s deleting nid=%d '%s' (attempt %d/%d)",
                status, nid, title, attempt, MAX_RETRIES,
            )
        except requests.exceptions.RequestException as exc:
            logger.warning(
                "  Request error deleting nid=%d '%s' (attempt %d/%d): %s",
                nid, title, attempt, MAX_RETRIES, exc,
            )

        if attempt < MAX_RETRIES:
            time.sleep(RETRY_DELAY_SECONDS)

    logger.error("  All %d delete attempts failed for nid=%d '%s'", MAX_RETRIES, nid, title)
    return False


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    args = parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args["debug"] else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    load_dotenv()
    config = load_config()

    session = requests.Session()
    session.auth = (config["username"], config["password"])
    session.headers.update({
        "Accept": "application/vnd.api+json",
        "Content-Type": "application/vnd.api+json",
    })
    session.verify = False

    # ------------------------------------------------------------------
    # --diff mode: show why two specific nodes don't auto-match
    # ------------------------------------------------------------------
    if args["diff_pair"]:
        keep_nid, discard_nid = args["diff_pair"]
        keep = fetch_node_by_nid(session, config["base_url"], config["content_type"], keep_nid)
        discard = fetch_node_by_nid(session, config["base_url"], config["content_type"], discard_nid)
        if not keep or not discard:
            sys.exit(1)
        show_diff(keep, discard)
        return

    # ------------------------------------------------------------------
    # --force-merge mode: merge two specific nodes by nid, skip detection
    # ------------------------------------------------------------------
    if args["force_merge"]:
        keep_nid, discard_nid = args["force_merge"]
        logger.info("Force-merge: keeping nid=%d, discarding nid=%d", keep_nid, discard_nid)

        keep = fetch_node_by_nid(session, config["base_url"], config["content_type"], keep_nid)
        discard = fetch_node_by_nid(session, config["base_url"], config["content_type"], discard_nid)
        if not keep or not discard:
            sys.exit(1)

        show_diff(keep, discard)
        changes = merge_into_primary(keep, [discard])

        if args["dry_run"]:
            logger.info("DRY RUN — no changes will be made")
            if changes:
                logger.info("Would PATCH nid=%d with: %s", keep_nid, json.dumps(changes, default=str))
            else:
                logger.info("No field changes needed for nid=%d", keep_nid)
            logger.info("Would DELETE nid=%d (uuid=%s)", discard_nid, discard["uuid"])
            return

        if changes:
            ok = patch_node(
                session, config["base_url"], config["content_type"],
                keep["uuid"], changes, keep["title"],
            )
            if not ok:
                logger.error("PATCH failed — aborting to avoid data loss")
                sys.exit(1)
        else:
            logger.info("No field changes needed for nid=%d", keep_nid)

        delete_node(
            session, config["base_url"], config["content_type"],
            discard["uuid"], discard_nid, discard["title"],
        )
        return

    # ------------------------------------------------------------------
    # Default mode: auto-detect duplicates across all nodes
    # ------------------------------------------------------------------
    nodes = fetch_all_nodes(session, config["base_url"], config["content_type"])
    duplicate_groups = find_duplicate_groups(nodes)

    if not duplicate_groups:
        logger.info("No duplicates found — nothing to do")
        return

    if args["dry_run"]:
        logger.info("DRY RUN — no changes will be made")

    merged_count = 0
    deleted_count = 0
    error_count = 0

    for group_index, group in enumerate(duplicate_groups, 1):
        primary, secondaries = choose_primary(group)
        title = primary["title"]

        logger.info(
            "[%d/%d] Duplicate: '%s'  primary=nid:%d  secondaries=%s",
            group_index,
            len(duplicate_groups),
            title,
            primary["nid"],
            [n["nid"] for n in secondaries],
        )

        changes = merge_into_primary(primary, secondaries)

        if args["dry_run"]:
            if changes:
                logger.info("  Would PATCH primary with: %s", json.dumps(changes, default=str))
            else:
                logger.info("  No field changes needed for primary")
            for secondary in secondaries:
                logger.info("  Would DELETE nid=%d (uuid=%s)", secondary["nid"], secondary["uuid"])
            continue

        # Apply PATCH only if there is something new to write
        if changes:
            patch_ok = patch_node(
                session, config["base_url"], config["content_type"],
                primary["uuid"], changes, title,
            )
            if not patch_ok:
                logger.error("  PATCH failed — skipping deletion of secondaries to avoid data loss")
                error_count += 1
                continue
            merged_count += 1
        else:
            logger.info("  No field changes needed for primary")

        # Delete all secondaries
        for secondary in secondaries:
            ok = delete_node(
                session, config["base_url"], config["content_type"],
                secondary["uuid"], secondary["nid"], title,
            )
            if ok:
                deleted_count += 1
            else:
                error_count += 1

    logger.info("=" * 60)
    logger.info("MERGE SUMMARY")
    logger.info("  Duplicate groups found : %d", len(duplicate_groups))
    logger.info("  Primary nodes patched  : %d", merged_count)
    logger.info("  Secondary nodes deleted: %d", deleted_count)
    logger.info("  Errors                 : %d", error_count)
    if error_count:
        logger.warning("Re-run to retry %d error(s)", error_count)


if __name__ == "__main__":
    main()
