## Problem

Running Azure OpenAI on every Drupal node daily is expensive and wasteful when most nodes haven't changed.

---

## Solution
Incremental Scanning Using Drupal's `changed` Timestamp

Drupal JSON:API exposes a `changed` attribute on every node (the last-modified timestamp).

Instead of tracking just a set of completed UUIDs, we store:

- When each node was last scanned
- What its `changed` timestamp was at that time

On subsequent runs, we only call the AI for:

1. **New nodes** — UUID not in our progress file  
2. **Modified nodes** — node's `changed` timestamp is newer than what we recorded  

This means:

- **Day 1** → `N` API calls  
- **Subsequent days** → only `(new + modified)` calls  

---

## Changes to `drupal_ai_scanner.py`

- Progress format updated:

  ```json
  {
    "uuid": {
      "scanned_at": "...",
      "node_changed": "..."
    }
  }
  ```

(instead of a flat set)

* Fetch now includes the `changed` field from Drupal
* Filter logic compares timestamps
* Backward-compatible migration of old progress files

---

## Optimization (Optional but Recommended)

An even cheaper approach:

Use Drupal's JSON:API filter to fetch only nodes where:

```text
changed > last_run_timestamp
```

This avoids paginating through all nodes entirely.

⚠️ Requires Drupal JSON:API date filter support (standard in most setups)

You can **combine both approaches** for maximum safety and efficiency.

---

## Rewrite Requirements

The updated `drupal_ai_scanner.py` should:

1. Store progress as:

   ```json
   {
     "uuid": {
       "scanned_at": "...",
       "node_changed": "..."
     }
   }
   ```

2. Fetch the `changed` timestamp from Drupal

3. Use a Drupal filter (`changed > last_run`) on subsequent runs

4. Fall back to local timestamp comparison if filtering fails

5. Auto-migrate old progress file format

---

## Key Changes

### `--full` Flag

Forces a complete rescan.

* With flag → scan all nodes
* Without flag → incremental scanning

---

### Progress Format Upgrade

Each node now stores:

```json
{
  "uuid-here": {
    "scanned_at": "2026-03-31 10:00:00",
    "node_changed": "2026-03-31T09:45:00+00:00"
  }
}
```

---

### Automatic Migration

Old progress format:

```json
{
  "completed": ["uuid1", "uuid2"]
}
```

Is automatically converted.

* Migrated nodes get:

  ```text
  node_changed = 1970-01-01
  ```
* Ensures they are rescanned if Drupal shows updates

---

### Incremental Fetch

On subsequent runs:

* Script sends:

  ```text
  changed > latest_timestamp
  ```

  via JSON:API filter

* If Drupal rejects the filter:

  * Falls back to fetching all nodes
  * Uses local timestamp comparison

---

### Improved `--dry-run`

Now labels each node as:

* `NEW`
* `MODIFIED`

So you can see exactly why it would be processed.

---

## Cost Impact

| Scenario                 | API calls   |
| ------------------------ | ----------- |
| Day 1 (first run)        | All N nodes |
| Day 2+ (no changes)      | 0           |
| Day 2+ (5 new, 3 edited) | 8           |
| `--full` flag            | All N nodes |

<br>
