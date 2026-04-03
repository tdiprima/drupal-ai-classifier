import json
from pathlib import Path
from drupal_ai_scanner import (
    compute_content_hash,
    load_config,
    fetch_all_nodes,
    requests,
)

config = load_config()

session = requests.Session()
session.auth = (config["username"], config["password"])
session.headers.update({"Accept": "application/vnd.api+json"})
session.verify = False

nodes = fetch_all_nodes(
    session,
    config["base_url"],
    config["content_type"],
)

node_map = {n["uuid"]: n for n in nodes}

pf = Path("ai_scan_progress.json")
data = json.loads(pf.read_text())

for uuid, entry in data["nodes"].items():
    if uuid in node_map:
        entry["content_hash"] = compute_content_hash(node_map[uuid])

pf.write_text(json.dumps(data, indent=2))

print(f"Backfilled hashes for {len(data['nodes'])} nodes")
