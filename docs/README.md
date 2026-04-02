## Here's what `--full` actually does vs. the default:

### `--full` (line 423–429)
1. Fetches **all nodes** from Drupal  
2. Then passes them through `filter_pending_nodes()` — which skips any node whose UUID is already tracked and whose `changed` timestamp hasn't advanced  

So yes, it **will skip already-scanned nodes correctly**.  
But it's doing extra work — **fetching everything first**.

---

### Default (no flags) — the smarter ongoing choice (lines 431–434)
- Asks Drupal for **only nodes changed since your most recent recorded timestamp**
- **Never fetches nodes you've already scanned**

---

## Bottom line:

| Command                              | When to use                                      |
|--------------------------------------|--------------------------------------------------|
| `python drupal_ai_scanner.py`        | Normal ongoing use — most efficient              |
| `python drupal_ai_scanner.py --full` | When you suspect missed nodes or want to verify coverage |
| `python drupal_ai_scanner.py --reset`| Start completely over                            |

---

For your routine **"check for updates"** workflow, just run it **without any flags**.  
The `--full` flag is a **safety net**, not the standard mode.

<br>
