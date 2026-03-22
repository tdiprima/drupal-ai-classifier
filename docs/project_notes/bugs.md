# Bug Log

## Template

### 2026-03-20 - BUG-001: "Deduped"

**Issue**: I erased too many records

**Root Cause**: Ran script `X` 3 times, then panicked and ran `drupal_dedup. py` again. It wasn't the proper way to dedupe the 2nd time. 

**Solution**: Gave the whole mess to Claude, who wrote `drupal_recovery.py`. 

**Prevention**: Don't panic. 

---

## The problem

I think I goofed. Originally, we uploaded records from xlsx to drupal if the values in colums "Vendor Name", "Product Name", AND "Description" were present. Then I decided to add rows where "Vendor Name" and "Product Name", had to be present, but "Description" was blank (because we already uploaded ones WITH the description. Then I ran a deduplicating program, and I think it deleted too many nodes.

---

✅ **Prompt Engineering Tip:**  
Whenever debugging data pipelines, structure prompts into **Context → Change Introduced → Problem → Task → Constraints → Output Format**. This helps the model reason through the failure chain more reliably.

---

## Solution

Root cause analysis — why the dedup deleted too many nodes
 
1.  Two legitimate import phases were treated as one duplicate set
 
Both `drupal_importer.py` and `import_no_description.py` use the same natural key: (vendor. lower(),
product. lower()).  If the dedup grouped all Drupal nodes by this key and kept only one per group, it would
 delete one node per product pair — even when both nodes were intentional and unique by phase.  
 
2.  Em-dash (—) vs.  ASCII dash mismatch in title parsing

Every node title is "{vendor} — {product}" (U+2014 em dash).  A dedup script parsing titles with a regular
 - or -- would fail to extract the vendor/product components correctly, causing incorrect groupings or 
false-positive duplicate detection. 
 
3.  Incomplete Drupal pagination

`fetch_all_nodes` pages 50 nodes at a time.  If the dedup didn't follow `links.next` all the way through, it
operated on a partial snapshot — nodes on later pages looked like "new duplicates of nothing" and got
deleted.  
 
4.  No cross-reference of both progress files 

The two importers wrote to separate progress files (`import_progress.json` and 
`import_nodesc_progress.json`).  A dedup unaware of this distinction couldn't tell which nodes were 
intentional phase-2 imports vs.  accidental duplicates.  
 
## Recovery plan — step by step
 
┌──────┬──────────────────────────────────────────────────────────────────────┐
│ Step │Action│
├──────┼──────────────────────────────────────────────────────────────────────┤
│ 1│ Run --dry-run to preview the gap before touching anything│
├──────┼──────────────────────────────────────────────────────────────────────┤
│ 2│ Review the printed missing records — confirm they match expectations │
├──────┼──────────────────────────────────────────────────────────────────────┤
│ 3│ Run live to re-import missing nodes│
├──────┼──────────────────────────────────────────────────────────────────────┤
│ 4│ Re-run as needed — progress is tracked in recovery_progress. json │
└──────┴──────────────────────────────────────────────────────────────────────┘

```sh
# Preview what would be re-imported
python drupal_recovery. py --dry-run
 
# Live recovery
python drupal_recovery.py

# If interrupted — safe to re-run; already-recovered nodes are skipped 
python drupal_recovery.py
```

Key design decisions in the script
 
- `build_expected_records()` calls both `load_and_merge()` (phase 1) and `load_no_description_records()` (phase
 2) and merges them — phase-1 records win on key collision since they carry more data.  
- `fetch_present_keys()` parses the em-dash title format exactly as the importers produce it, preventing
the mismatch that likely caused the original problem. 
- `_key_from_title()` partitions on U+2014 explicitly — no ambiguity with ASCII dashes. 
- All Drupal interaction reuses `post_node_with_retry`, `build_payload`, `load_progress`, `mark_complete`, and 
`save_progress` directly from `drupal_importer. py` — no logic is duplicated.  
- The recovery progress file (`recovery_progress. json`) is separate from both existing progress files, so
it doesn't interfere with the original import state.  

---

❯ Yeah, but maybe the deduplication process was faulty - like if it looks like it's the same node but it 
keeps the one with 9 fields filled in and drops the one with 4 fields filled in - they may have been 
different records?Like, do the fields that exist look the same?If yes, only then it should be a dupe. 
 If no, then it's not a dupe. The original deduplication didn't check the field values.  

⏺ Good point.  The dedup's real flaw was using "field count" as a proxy for identity — it kept the richer
node without checking whether the values of the shared fields actually agreed.  Two nodes with the same
title but different field_sites_used, field_status, etc.  are distinct records, not duplicates. 

The recovery script I wrote handles the case where nodes are completely missing.  But there's a second
failure mode: the node still exists in Drupal, but it's the wrong version — the dedup kept the one with
more fields, discarding data that only existed in the other copy. 

I need to read the existing scripts again to make sure the new script imports only what's needed. 

---

`drupal_dedup_patch.py`

What the new script does differently 
 
The core insight is captured in `build_restoration_attributes()`:
 
## Values already match — no patch needed 

```py
if fields_are_equivalent(field, record, drupal_attrs): 
    continue
```

For every field, it asks: does the Drupal value match the XLSX value? Only if they differ (or Drupal is
empty) does it queue a PATCH.  This means:
 
┌──────────────────────────┬──────────────────────────┬──────────────────────────────────────────────┐ 
│ Scenario │ Old dedup behaviour│New behaviour │
├──────────────────────────┼──────────────────────────┼──────────────────────────────────────────────┤ 
│ Same title, same field │ Correctly identified as│ Detects no diff → no-op│
│ values │ duplicate││
├──────────────────────────┼──────────────────────────┼──────────────────────────────────────────────┤ 
│ Same title, different│ Wrongly deleted one copy │ Detects diff → patches the surviving node to │ 
│ field values ││restore lost data │ 
├──────────────────────────┼──────────────────────────┼──────────────────────────────────────────────┤ 
│ Title missing entirely │ Not handled│ Reported; drupal_recovery. py handles │
│ from Drupal││ re-creation│ 
└──────────────────────────┴──────────────────────────┴──────────────────────────────────────────────┘
 
## How fields_are_equivalent() works

Both sides go through the same normalization pipeline before comparison: 
 
- field_sites_used — compared as sorted, lowercase, comma-joined sets
- Date fields — both coerced to YYYY-MM-DD
- List/select fields — both run through normalize_list_value() (same fuzzy matching the importer uses) 
- Everything else — str(). strip(). lower()
 
This prevents false positives from case or format differences (e. g. , "Active" vs "active", "2024-01-15 
00:00:00" vs "2024-01-15"). 
 
## Recommended run order
 
```sh
# 1.  Preview what would be patched (no writes) 
python drupal_dedup_patch. py --dry-run 
 
# 2.  Live patch — restores field values the dedup overwrote/lost 
python drupal_dedup_patch. py 
 
# 3.  Re-create nodes that are completely absent
python drupal_recovery. py --dry-run
python drupal_recovery. py
```

---

❯ So I should not run `drupal_recovery.py` at all, right?Just `drupal_dedup_patch.py`?

⏺ Not quite — they cover different failure modes and you likely need both.  
 
`drupal_dedup_patch.py` only patches nodes that still exist in Drupal but have wrong or missing field
values.  When it finds a node that is completely absent from Drupal, it logs it and skips it — it never
creates nodes.  

`drupal_recovery.py` handles the other case: nodes the dedup deleted entirely, where no surviving node 
exists at all. 
 
The dedup could have produced both outcomes: 
 
┌────────────────────────────────────────────────────────────┬───────────────────────────────────────┐ 
│ What the dedup did │ Which script fixes it │
├────────────────────────────────────────────────────────────┼───────────────────────────────────────┤ 
│ Kept Node A (9 fields), deleted Node B (4 fields) — but A│ drupal_dedup_patch.py patches Node A│
│ and B had different values → Node B's unique data is gone│ to restore B's lost data│ 
├────────────────────────────────────────────────────────────┼───────────────────────────────────────┤ 
│ Deleted a node and kept nothing (e. g. , mis-identified it │ drupal_recovery.py re-creates the │
│ as a duplicate of something on a different page it never │ node from scratch │ 
│ fetched) │ │ 
└────────────────────────────────────────────────────────────┴───────────────────────────────────────┘
 
The summary line at the end of drupal_dedup_patch. py will tell you how many nodes fall into each bucket: 
 
Not in Drupal (missing): 12 ← run drupal_recovery. py for these 
Patched (data restored): 47 ← handled
Already correct: 203← no action needed 
 
So the safe sequence is: 

```sh
# Step 1 — fix nodes that exist but have wrong/missing data
python drupal_dedup_patch.py --dry-run 
python drupal_dedup_patch.py
 
# Step 2 — re-create nodes that are completely gone
python drupal_recovery.py --dry-run
python drupal_recovery.py
```

If the "Not in Drupal (missing)" count is zero after step 1, then you don't need step 2 at all. 

---

❯ When `drupal_dedup_patch.py` says "CONFLICT", is it fixing the node? 
 
⏺ Yes — both CONFLICT and MISSING result in a PATCH.  The logging just distinguishes why the field needs
fixing:
 
- MISSING → Drupal is empty, XLSX has a value
- CONFLICT → Drupal has a value, but it disagrees with XLSX
 
In both cases the code falls through to the same block that adds the field to attributes, which then gets
 PATCHed with the XLSX value:

```py
if drupal_norm: 
    logger.info("CONFLICT...")  # different value in Drupal
else:
    logger.info("MISSING...")  # empty in Drupal
 
# Both cases reach this — the field is added to the PATCH either way 
if field == "field_sites_used":
    attributes[field] = [{"value": s} for s in valid]
elif field in DATE_FIELDS:
    attributes[field] = normalized 
...  
```

One thing worth being aware of: in the CONFLICT case the script unconditionally trusts XLSX over whatever
 Drupal currently holds.  That's the right assumption here since XLSX is the authoritative source — but if
 someone manually edited a node in Drupal after the original import, those manual edits would be 
overwritten.  If that's a concern, run --dry-run first and review the CONFLICT lines before committing. 

<br>
