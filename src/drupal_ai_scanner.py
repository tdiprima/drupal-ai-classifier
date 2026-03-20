#!/usr/bin/env python3
"""
drupal_ai_scanner.py

Fetches Drupal nodes of type `new_product`, runs each through the Azure OpenAI
AI scanner, and PATCHes the results back onto the node.

Field mappings (Drupal ← AI scanner output):
    field_ai_application      ← has_ai        ("yes" / "no")
    field_confidence          ← confidence    ("low" / "medium" / "high")
    field_reason              ← reason        (truncated to 256 chars)
    field_risk_review_required ← needs_review ("yes" / "no")

Progress is persisted to a JSON file so interrupted runs resume cleanly.
The first run processes only 10 nodes; subsequent runs process all remaining.

Usage:
    python drupal_ai_scanner.py                  # first run: 10 nodes
    python drupal_ai_scanner.py                  # resume: all remaining
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
from dotenv import load_dotenv
from openai import AzureOpenAI

from ai_scanner_core import check_for_ai, configure_logging

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

logger = logging.getLogger(__name__)

ENV_FILE = Path(__file__).parent.parent / ".env"
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
        "limit": limit,
    }


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

def load_config() -> dict:
    """Load and validate all required environment variables from .env."""
    load_dotenv(ENV_FILE)

    required = [
        "DRUPAL_BASE_URL",
        "DRUPAL_USERNAME",
        "DRUPAL_PASSWORD",
        "DRUPAL_CONTENT_TYPE",
        "AZURE_OPENAI_ENDPOINT",
        "AZURE_OPENAI_API_KEY",
        "AZURE_OPENAI_DEPLOYMENT",
    ]
    missing = [k for k in required if not os.environ.get(k)]
    if missing:
        logger.error("Missing required environment variables: %s", ", ".join(missing))
        sys.exit(1)

    return {
        "base_url": os.environ["DRUPAL_BASE_URL"].rstrip("/"),
        "username": os.environ["DRUPAL_USERNAME"],
        "password": os.environ["DRUPAL_PASSWORD"],
        "content_type": os.environ["DRUPAL_CONTENT_TYPE"],
        "azure_endpoint": os.environ["AZURE_OPENAI_ENDPOINT"],
        "azure_api_key": os.environ["AZURE_OPENAI_API_KEY"],
        "azure_deployment": os.environ["AZURE_OPENAI_DEPLOYMENT"],
    }


# ---------------------------------------------------------------------------
# Progress tracking
# ---------------------------------------------------------------------------

def load_progress(progress_file: Path) -> dict:
    """
    Load persisted progress from disk.
    Returns a dict with 'completed' (set of UUIDs) and 'is_first_run' flag.
    """
    if not progress_file.exists():
        return {"completed": set(), "is_first_run": True}
    try:
        data = json.loads(progress_file.read_text(encoding="utf-8"))
        return {
            "completed": set(data.get("completed", [])),
            "is_first_run": False,
        }
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Could not read progress file: %s — starting fresh", exc)
        return {"completed": set(), "is_first_run": True}


def save_progress(progress_file: Path, completed: set, stats: dict) -> None:
    """Atomically write progress to disk via a temp file."""
    data = {
        "last_updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "completed_count": len(completed),
        "completed": sorted(completed),
        "last_run": stats,
    }
    tmp = progress_file.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(progress_file)
    except OSError as exc:
        logger.error("Failed to write progress file: %s", exc)


# ---------------------------------------------------------------------------
# Drupal: fetching nodes
# ---------------------------------------------------------------------------

def fetch_nodes(
    session: requests.Session,
    base_url: str,
    content_type: str,
    completed: set,
) -> list[dict]:
    """
    Fetch all nodes of the given content type, returning only those not yet
    in the completed set. Handles JSON:API pagination automatically.
    """
    pending = []
    url = f"{base_url}/jsonapi/node/{content_type}"
    params = {
        f"fields[node--{content_type}]": (
            "id,title,field_vendor_name,field_product_name,field_description"
        ),
        "page[limit]": PAGE_SIZE,
    }

    page = 0
    while url:
        page += 1
        response = session.get(url, params=params if page == 1 else None, timeout=30)
        if response.status_code != 200:
            logger.error("Failed to fetch nodes (HTTP %s)", response.status_code)
            sys.exit(1)

        body = response.json()
        for node in body.get("data", []):
            uuid = node.get("id", "")
            if uuid and uuid not in completed:
                attrs = node.get("attributes", {})
                pending.append({
                    "uuid": uuid,
                    "title": attrs.get("title", ""),
                    "vendor": attrs.get("field_vendor_name") or "",
                    "product": attrs.get("field_product_name") or "",
                    "description": attrs.get("field_description") or "",
                })

        url = body.get("links", {}).get("next", {}).get("href")
        params = None  # params are encoded in the next link

    logger.info("Found %d node(s) not yet scanned", len(pending))
    return pending


# ---------------------------------------------------------------------------
# AI result → Drupal attribute mapping
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

    # UNKNOWN is treated conservatively as a potential AI application
    ai_application = "yes" if has_ai in ("YES", "UNKNOWN") else "no"
    needs_review = "yes" if has_ai in ("YES", "UNKNOWN") else "no"

    # Clamp confidence to known Drupal keys
    if confidence not in ("low", "medium", "high"):
        confidence = "low"

    # Truncate reason to 256 characters at a word boundary
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
) -> bool:
    """PATCH a single Drupal node. Returns True on success."""
    url = f"{base_url}/jsonapi/node/{content_type}/{uuid}"
    try:
        response = session.patch(url, json=payload, timeout=30)
        response.raise_for_status()
        return True
    except requests.exceptions.HTTPError as exc:
        logger.error(
            "HTTP %s patching '%s': %s",
            exc.response.status_code, title, exc.response.text[:400],
        )
    except requests.exceptions.RequestException as exc:
        logger.error("Request error patching '%s': %s", title, exc)
    return False


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
    completed: set = progress["completed"]
    is_first_run: bool = progress["is_first_run"]

    # First run defaults to 10 nodes as a sanity check; subsequent runs get all remaining
    limit = args["limit"]
    if limit is None and is_first_run:
        limit = FIRST_RUN_LIMIT
        logger.info("First run — limiting to %d node(s). Use --limit to override.", limit)

    # Build the HTTP session for Drupal
    session = requests.Session()
    session.auth = (config["username"], config["password"])
    session.headers.update({
        "Accept": "application/vnd.api+json",
        "Content-Type": "application/vnd.api+json",
    })
    session.verify = False

    # Build the Azure OpenAI client
    ai_client = AzureOpenAI(
        azure_endpoint=config["azure_endpoint"],
        api_key=config["azure_api_key"],
        api_version="2024-02-15-preview",
    )
    deployment = config["azure_deployment"]

    logger.info("Fetching pending nodes from Drupal...")
    nodes = fetch_nodes(session, config["base_url"], config["content_type"], completed)

    if limit:
        nodes = nodes[:limit]
        logger.info("Processing %d node(s) this run", len(nodes))

    if not nodes:
        logger.info("Nothing to process — all nodes already scanned")
        return

    if args["dry_run"]:
        logger.info("DRY RUN — no nodes will be patched")
        for node in nodes:
            logger.info("  Would scan: %s", node["title"])
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
            logger.info("[%d/%d] %s", i, total, title)

            # Build the entry dict expected by check_for_ai
            entry = {
                "vendor": node["vendor"],
                "product": node["product"],
                "description": node["description"],
            }

            ai_result = check_for_ai(ai_client, deployment, entry)
            logger.info(
                "  → has_ai=%s  confidence=%s",
                ai_result["has_ai"], ai_result["confidence"],
            )

            attributes = map_ai_result_to_attributes(ai_result)
            if attributes is None:
                logger.error("  AI error for '%s' — skipping PATCH, will retry next run", title)
                failed += 1
                continue

            payload = build_patch_payload(config["content_type"], uuid, attributes)

            if patch_node(session, config["base_url"], config["content_type"], uuid, payload, title):
                succeeded += 1
                completed.add(uuid)
                stats.update({"succeeded": succeeded, "failed": failed})
                save_progress(progress_file, completed, stats)
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
        save_progress(progress_file, completed, stats)

        logger.info("=" * 60)
        logger.info("SCAN SUMMARY")
        logger.info("  Processed this run : %d", total)
        logger.info("  Succeeded          : %d", succeeded)
        logger.info("  Failed             : %d", failed)
        logger.info("  Total completed    : %d", len(completed))
        logger.info("  Elapsed            : %.1fs", elapsed)
        if failed:
            logger.warning("Re-run to retry %d failed node(s)", failed)


if __name__ == "__main__":
    main()
