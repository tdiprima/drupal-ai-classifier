#!/usr/bin/env python3
from __future__ import annotations
"""
drupal_ai_scanner.py

Fetches Drupal nodes of type `new_product`, runs each through the Azure OpenAI
AI scanner, and PATCHes the results back onto the node.

**Incremental mode (default):** After the initial full scan, only new or
modified nodes are processed.  Each scanned node's Drupal `changed` timestamp
is recorded; on subsequent runs the script asks Drupal for nodes changed after
the most recent recorded timestamp, so unchanged nodes are never re-fetched or
re-scanned.

Field mappings (Drupal <- AI scanner output):
    field_ai_application       <- has_ai        ("yes" / "no")
    field_confidence           <- confidence    ("low" / "medium" / "high")
    field_reason               <- reason        (truncated to 256 chars)
    field_risk_review_required <- needs_review  ("yes" / "no")

Progress is persisted to a JSON file so interrupted runs resume cleanly.
The first run processes only 10 nodes; subsequent runs process all remaining.

Usage:
    python drupal_ai_scanner.py                  # incremental: new/changed only
    python drupal_ai_scanner.py --full           # force full rescan of all nodes
    python drupal_ai_scanner.py --limit 25       # explicit batch size
    python drupal_ai_scanner.py --reset          # clear progress and restart
    python drupal_ai_scanner.py --dry-run        # preview without patching
    python drupal_ai_scanner.py --debug          # verbose AI responses
    python drupal_ai_scanner.py --help

Required .env variables:
    DRUPAL_BASE_URL
    DRUPAL_USERNAME
    DRUPAL_PASSWORD
    DRUPAL_CONTENT_TYPE
    AZURE_OPENAI_ENDPOINT
    AZURE_OPENAI_API_KEY
    AZURE_OPENAI_DEPLOYMENT
"""

import json
import logging
import os
import sys
import time
from datetime import datetime
from pathlib import Path

import requests
import urllib3
from openai import OpenAI

from ai_scanner_core import check_for_ai, configure_logging
from drupal_importer import load_config as load_drupal_config

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

logger = logging.getLogger(__name__)

DEFAULT_PROGRESS_FILE = Path(__file__).parent.parent / "ai_scan_progress.json"
FIRST_RUN_LIMIT = 10
PAGE_SIZE = 50


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

def parse_args() -> dict:
    """Parse command-line arguments from sys.argv."""
    args = sys.argv[1:]

    if "--help" in args or "-h" in args:
        print(__doc__)
        sys.exit(0)

    dry_run = "--dry-run" in args
    debug = "--debug" in args
    reset = "--reset" in args
    full = "--full" in args
    limit = None

    if "--limit" in args:
        idx = args.index("--limit")
        if idx + 1 < len(args):
            try:
                limit = int(args[idx + 1])
            except ValueError:
                logger.error("--limit requires an integer argument")
                sys.exit(1)

    return {
        "dry_run": dry_run,
        "debug": debug,
        "reset": reset,
        "full": full,
        "limit": limit,
    }


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

def load_config() -> dict:
    """Load Drupal config and validate Azure OpenAI environment variables."""
    config = load_drupal_config()

    azure_required = ["AZURE_OPENAI_ENDPOINT", "AZURE_OPENAI_API_KEY", "AZURE_OPENAI_DEPLOYMENT"]
    missing = [k for k in azure_required if not os.environ.get(k)]
    if missing:
        logger.error("Missing required environment variables: %s", ", ".join(missing))
        sys.exit(1)

    config.update({
        "azure_endpoint": os.environ["AZURE_OPENAI_ENDPOINT"],
        "azure_api_key": os.environ["AZURE_OPENAI_API_KEY"],
        "azure_deployment": os.environ["AZURE_OPENAI_DEPLOYMENT"],
    })
    return config


# ---------------------------------------------------------------------------
# Progress tracking
# ---------------------------------------------------------------------------

def load_progress(progress_file: Path) -> dict:
    """
    Load persisted progress from disk.

    Returns a dict with:
        'nodes'        — {uuid: {"scanned_at": str, "node_changed": str}}
        'is_first_run' — True if no progress file existed
    """
    if not progress_file.exists():
        return {"nodes": {}, "is_first_run": True}
    try:
        data = json.loads(progress_file.read_text(encoding="utf-8"))
        return _parse_progress_data(data)
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Could not read progress file: %s — starting fresh", exc)
        return {"nodes": {}, "is_first_run": True}


def _parse_progress_data(data: dict) -> dict:
    """Parse progress data, migrating from old format if necessary."""
    # New format: nodes is a dict of {uuid: {scanned_at, node_changed}}
    if "nodes" in data and isinstance(data["nodes"], dict):
        return {"nodes": data["nodes"], "is_first_run": False}

    # Old format: "completed" is a list of UUID strings — migrate
    if "completed" in data and isinstance(data["completed"], list):
        logger.info("Migrating old progress format to incremental format")
        last_updated = data.get("last_updated", "1970-01-01T00:00:00+00:00")
        nodes = {}
        for uuid in data["completed"]:
            nodes[uuid] = {
                "scanned_at": last_updated,
                "node_changed": "1970-01-01T00:00:00+00:00",
            }
        return {"nodes": nodes, "is_first_run": False}

    return {"nodes": {}, "is_first_run": True}


def save_progress(progress_file: Path, nodes: dict, stats: dict) -> None:
    """Atomically write progress to disk via a temp file."""
    data = {
        "last_updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "completed_count": len(nodes),
        "nodes": nodes,
        "last_run": stats,
    }
    tmp = progress_file.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(progress_file)
    except OSError as exc:
        logger.error("Failed to write progress file: %s", exc)


def latest_scanned_timestamp(nodes: dict) -> str | None:
    """
    Return the most recent 'node_changed' value across all tracked nodes.
    Used to build a Drupal filter for incremental fetches.
    """
    if not nodes:
        return None
    timestamps = [
        entry["node_changed"]
        for entry in nodes.values()
        if entry.get("node_changed") and entry["node_changed"] != "1970-01-01T00:00:00+00:00"
    ]
    return max(timestamps) if timestamps else None


# ---------------------------------------------------------------------------
# Drupal: fetching nodes
# ---------------------------------------------------------------------------

def fetch_all_nodes(
    session: requests.Session,
    base_url: str,
    content_type: str,
) -> list[dict]:
    """
    Fetch all nodes of the given content type, including the `changed`
    timestamp. Returns every node without filtering.
    """
    return _paginate_nodes(session, base_url, content_type, extra_params={})


def fetch_nodes_changed_since(
    session: requests.Session,
    base_url: str,
    content_type: str,
    since: str,
) -> list[dict]:
    """
    Fetch only nodes whose Drupal `changed` timestamp is greater than `since`.
    Falls back to a full fetch if the server rejects the filter.
    """
    filter_params = {
        "filter[changed][condition][path]": "changed",
        "filter[changed][condition][operator]": ">",
        "filter[changed][condition][value]": since,
    }
    try:
        nodes = _paginate_nodes(session, base_url, content_type, extra_params=filter_params)
        return nodes
    except RuntimeError:
        logger.warning("Drupal rejected the date filter — falling back to full fetch")
        return fetch_all_nodes(session, base_url, content_type)


def _paginate_nodes(
    session: requests.Session,
    base_url: str,
    content_type: str,
    extra_params: dict,
) -> list[dict]:
    """
    Walk JSON:API pagination and return parsed node dicts.
    Raises RuntimeError if the first page returns a non-200 status.
    """
    nodes = []
    url = f"{base_url}/jsonapi/node/{content_type}"
    params = {
        f"fields[node--{content_type}]": (
            "id,title,changed,"
            "field_vendor_name,field_product_name,field_description"
        ),
        "page[limit]": PAGE_SIZE,
    }
    params.update(extra_params)

    page = 0
    while url:
        page += 1
        response = session.get(url, params=params if page == 1 else None, timeout=30)

        if response.status_code != 200:
            if page == 1:
                raise RuntimeError(f"HTTP {response.status_code}")
            logger.error("Failed to fetch page %d (HTTP %s)", page, response.status_code)
            break

        body = response.json()
        for node in body.get("data", []):
            uuid = node.get("id", "")
            if not uuid:
                continue
            attrs = node.get("attributes", {})
            nodes.append({
                "uuid": uuid,
                "title": attrs.get("title", ""),
                "vendor": attrs.get("field_vendor_name") or "",
                "product": attrs.get("field_product_name") or "",
                "description": attrs.get("field_description") or "",
                "changed": attrs.get("changed") or "",
            })

        url = body.get("links", {}).get("next", {}).get("href")
        params = None

    return nodes


MIGRATION_SENTINEL = "1970-01-01T00:00:00+00:00"


def filter_pending_nodes(
    fetched_nodes: list[dict],
    tracked_nodes: dict,
) -> list[dict]:
    """
    From a list of fetched Drupal nodes, return only those that need scanning:
      - UUID not in tracked_nodes (new)
      - node's `changed` timestamp is newer than what we recorded (modified)

    Nodes migrated from the old progress format have a sentinel timestamp.
    These were already scanned, so backfill their recorded timestamp from
    Drupal's current value instead of re-scanning.
    """
    pending = []
    for node in fetched_nodes:
        uuid = node["uuid"]
        if uuid not in tracked_nodes:
            pending.append(node)
            continue
        recorded_changed = tracked_nodes[uuid].get("node_changed", "")
        if recorded_changed == MIGRATION_SENTINEL:
            tracked_nodes[uuid]["node_changed"] = node["changed"]
            continue
        if node["changed"] and node["changed"] > recorded_changed:
            pending.append(node)
    return pending


# ---------------------------------------------------------------------------
# AI result -> Drupal attribute mapping
# ---------------------------------------------------------------------------

def map_ai_result_to_attributes(ai_result: dict) -> dict | None:
    """
    Convert ai_scanner_core output to Drupal field values.
    Returns None if the AI call errored (node should not be marked complete).
    """
    has_ai = ai_result.get("has_ai", "ERROR")
    confidence = ai_result.get("confidence", "LOW").lower()
    reason = ai_result.get("reason", "")

    if has_ai == "ERROR":
        return None

    ai_application = "yes" if has_ai in ("YES", "UNKNOWN") else "no"
    needs_review = "yes" if has_ai in ("YES", "UNKNOWN") else "no"

    if confidence not in ("low", "medium", "high"):
        confidence = "low"

    if len(reason) > 256:
        reason = reason[:253].rsplit(" ", 1)[0] + "..."

    return {
        "field_ai_application": ai_application,
        "field_confidence": confidence,
        "field_reason": reason,
        "field_risk_review_required": needs_review,
    }


# ---------------------------------------------------------------------------
# Drupal: patching nodes
# ---------------------------------------------------------------------------

def build_patch_payload(content_type: str, uuid: str, attributes: dict) -> dict:
    """Wrap field attributes in a JSON:API PATCH envelope."""
    return {
        "data": {
            "type": f"node--{content_type}",
            "id": uuid,
            "attributes": attributes,
        }
    }


def patch_node(
    session: requests.Session,
    base_url: str,
    content_type: str,
    uuid: str,
    payload: dict,
    title: str,
) -> str | None:
    """PATCH a single Drupal node. Returns the updated `changed` timestamp on success, None on failure."""
    url = f"{base_url}/jsonapi/node/{content_type}/{uuid}"
    try:
        response = session.patch(url, json=payload, timeout=30)
        response.raise_for_status()
        return response.json().get("data", {}).get("attributes", {}).get("changed", "")
    except requests.exceptions.HTTPError as exc:
        logger.error(
            "HTTP %s patching '%s': %s",
            exc.response.status_code, title, exc.response.text[:400],
        )
    except requests.exceptions.RequestException as exc:
        logger.error("Request error patching '%s': %s", title, exc)
    return None


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    args = parse_args()
    configure_logging(args["debug"])

    config = load_config()
    progress_file = DEFAULT_PROGRESS_FILE

    if args["reset"]:
        if progress_file.exists():
            progress_file.unlink()
            logger.info("Progress cleared — starting from scratch")

    progress = load_progress(progress_file)
    tracked_nodes: dict = progress["nodes"]
    is_first_run: bool = progress["is_first_run"]

    limit = args["limit"]
    if limit is None and is_first_run:
        limit = FIRST_RUN_LIMIT
        logger.info("First run — limiting to %d node(s). Use --limit to override.", limit)

    session = requests.Session()
    session.auth = (config["username"], config["password"])
    session.headers.update({
        "Accept": "application/vnd.api+json",
        "Content-Type": "application/vnd.api+json",
    })
    session.verify = False

    ai_client = OpenAI(
        base_url=config["azure_endpoint"],
        api_key=config["azure_api_key"],
    )
    deployment = config["azure_deployment"]

    # ----- Fetch nodes: incremental or full -----
    force_full = args["full"] or is_first_run
    since = latest_scanned_timestamp(tracked_nodes)

    if force_full or since is None:
        logger.info("Fetching all nodes from Drupal...")
        fetched = fetch_all_nodes(session, config["base_url"], config["content_type"])
    else:
        logger.info("Incremental scan — fetching nodes changed since %s", since)
        fetched = fetch_nodes_changed_since(
            session, config["base_url"], config["content_type"], since,
        )

    nodes = filter_pending_nodes(fetched, tracked_nodes)

    logger.info("Found %d node(s) needing AI scan", len(nodes))

    if limit:
        nodes = nodes[:limit]
        logger.info("Processing %d node(s) this run", len(nodes))

    if not nodes:
        logger.info("Nothing to process — all nodes are up to date")
        return

    if args["dry_run"]:
        logger.info("DRY RUN — no nodes will be patched")
        for node in nodes:
            uuid = node["uuid"]
            status = "NEW" if uuid not in tracked_nodes else "MODIFIED"
            logger.info("  [%s] %s", status, node["title"])
        return

    total = len(nodes)
    succeeded = 0
    failed = 0
    start_time = time.monotonic()

    stats: dict = {"started": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}

    try:
        for i, node in enumerate(nodes, 1):
            uuid = node["uuid"]
            title = node["title"]
            status = "NEW" if uuid not in tracked_nodes else "MODIFIED"
            logger.info("[%d/%d] [%s] %s", i, total, status, title)

            entry = {
                "vendor": node["vendor"],
                "product": node["product"],
                "description": node["description"],
            }

            ai_result = check_for_ai(ai_client, deployment, entry)
            logger.info(
                "  -> has_ai=%s  confidence=%s",
                ai_result["has_ai"], ai_result["confidence"],
            )

            attributes = map_ai_result_to_attributes(ai_result)
            if attributes is None:
                logger.error("  AI error for '%s' — skipping PATCH, will retry next run", title)
                failed += 1
                continue

            payload = build_patch_payload(config["content_type"], uuid, attributes)

            updated_changed = patch_node(
                session, config["base_url"], config["content_type"], uuid, payload, title,
            )
            if updated_changed is not None:
                succeeded += 1
                tracked_nodes[uuid] = {
                    "scanned_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "node_changed": updated_changed or node["changed"],
                }
                stats.update({"succeeded": succeeded, "failed": failed})
                save_progress(progress_file, tracked_nodes, stats)
                logger.debug("  Progress saved")
            else:
                failed += 1

    except KeyboardInterrupt:
        logger.warning("Interrupted by user (Ctrl+C)")

    finally:
        elapsed = time.monotonic() - start_time
        stats.update({
            "succeeded": succeeded,
            "failed": failed,
            "finished": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "elapsed_seconds": round(elapsed, 1),
        })
        save_progress(progress_file, tracked_nodes, stats)

        logger.info("=" * 60)
        logger.info("SCAN SUMMARY")
        logger.info("  Processed this run : %d", total)
        logger.info("  Succeeded          : %d", succeeded)
        logger.info("  Failed             : %d", failed)
        logger.info("  Total tracked      : %d", len(tracked_nodes))
        logger.info("  Elapsed            : %.1fs", elapsed)
        if failed:
            logger.warning("Re-run to retry %d failed node(s)", failed)


if __name__ == "__main__":
    main()
