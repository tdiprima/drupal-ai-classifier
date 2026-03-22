# Drupal Deduplication Bug & Recovery
## 🚨 What Actually Happened

You accidentally nuked a bunch of legit Drupal nodes during a dedup pass.

**Why?** Because two different imports looked like duplicates to the script.

**Import phases:**

1️⃣ **Phase 1 import**

* Vendor Name
* Product Name
* Description ✅

2️⃣ **Phase 2 import**

* Vendor Name
* Product Name
* Description ❌ (blank)

Then the dedup script got run **multiple times in panic mode**.

**Result:**

* Valid phase-2 nodes got deleted 💀
* Some nodes survived but had **fields overwritten or lost data**

---

# 🧠 Why This Bug Happened

### 1️⃣ One Key To Rule Them All

Both imports used the same key:

```
(vendor.lower(), product.lower())
```

So Drupal thought:

> "Oh cool, duplicates! Delete stuff!"

But they were **intentional duplicates from two import phases**.

---

### 2️⃣ Title Parsing Was Scuffed

Titles used an **em dash**:

```
Vendor — Product
```

But the dedup script probably parsed like:

```
Vendor - Product
or
Vendor -- Product
```

So vendor/product extraction got messy.

Unicode strikes again 💀

---

### 3️⃣ Pagination Was Broken

The script only fetched:

```
first 50 nodes
```

Everything after that looked like **new duplicates later**, which triggered deletions.

---

### 4️⃣ Progress Files Were Ignored

The script didn't check:

```
import_progress.json
import_nodesc_progress.json
```

So it had **no idea what had already been imported**.

---

### 5️⃣ Dedup Logic Was Too Dumb

It decided which node to keep based on:

> "Which node has more fields?"

Instead of verifying that **field values actually matched**.

So it sometimes kept the "richer" node but **lost unique data from the other one**.

---

# 🛠️ Recovery Scripts

Two scripts fix **different damage types**.

Think of them like:

* **Patch script = fix broken nodes**
* **Recovery script = resurrect deleted nodes**

---

## 🔧 Script 1 — `drupal_dedup_patch.py`

Purpose: **Fix existing nodes with wrong or missing fields**

This script:

* Treats the **XLSX file as truth**
* Updates nodes when Drupal values are wrong
* Skips nodes already matching

### Smart stuff it does

* Normalizes fields
* Sorts site lists
* Fixes date formats
* Handles fuzzy lists

### Logs you'll see

**MISSING**

```
Drupal field empty
```

**CONFLICT**

```
Drupal value ≠ XLSX value
```

Both get patched.

### Run it safely

Preview changes first:

```
python drupal_dedup_patch.py --dry-run
```

Then apply fixes:

```
python drupal_dedup_patch.py
```

Check the summary:

```
Not in Drupal (missing): 12
```

If this number is **> 0**, you'll need the recovery script.

---

# ♻️ Script 2 — `drupal_recovery.py`

Purpose: **Recreate nodes that got completely deleted**

What it does:

* Combines both import datasets
* Phase-1 data wins on collisions
* Uses **correct em-dash parsing**
* Tracks progress in:

```
recovery_progress.json
```

Safe to rerun (idempotent).

### Run it

Preview:

```
python drupal_recovery.py --dry-run
```

Then execute:

```
python drupal_recovery.py
```

---

# ✅ Safe Recovery Order

### Step 1 — Patch broken nodes

```
python drupal_dedup_patch.py --dry-run
python drupal_dedup_patch.py
```

### Step 2 — Recover deleted nodes (if needed)

```
python drupal_recovery.py --dry-run
python drupal_recovery.py
```

---

# 🧯 How To Avoid This Next Time

**Rule #1**

> Don't panic-run scripts.

Seriously.

---

**Rule #2**
Always check `--dry-run` output.

---

**Rule #3**
Dedup should verify **field values**, not just **field count**.

---

**Rule #4**
Unicode dashes are evil.

Always normalize:

```
—  (em dash)
–  (en dash)
-  (hyphen)
```

---

# TL;DR

You had:

```
2 import phases
+ 1 naive dedup script
+ panic re-runs
= data chaos
```

Recovery strategy:

```
1️⃣ Patch existing nodes
2️⃣ Recreate missing nodes
```

And next time:

```
--dry-run everything first
```

Your future self will thank you. 💀

<br>
