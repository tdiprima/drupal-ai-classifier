# Drupal Deduplication Bug and Recovery

## Bug Overview (BUG-001: "Deduped")
- **Issue**: Excessive node deletion during deduplication after two import phases into Drupal.
  - Phase 1: Imported XLSX rows with "Vendor Name", "Product Name", **and** "Description".
  - Phase 2: Imported rows with "Vendor Name" **and** "Product Name" (Description blank).
- **Trigger**: Ran dedup script (`drupal_dedup.py`) multiple times after panicking from repeated script executions.
- **Impact**: Legitimate phase-2 nodes deleted; some surviving nodes had overwritten/incorrect fields (e.g., kept richer node but lost unique data).

## Root Causes
1. **Single Natural Key**: Both phases used `(vendor.lower(), product.lower())`, treating intentional duplicates as errors.
2. **Title Parsing Mismatch**: Titles use em-dash (U+2014 "—"), but dedup likely used ASCII `-`/`--`, causing faulty vendor/product extraction.
3. **Incomplete Pagination**: Fetched only first 50 nodes, missing later ones treated as "new duplicates".
4. **No Progress File Cross-Check**: Ignored separate progress files (`import_progress.json`, `import_nodesc_progress.json`).
5. **Flawed Dedup Logic**: Used field count as proxy (kept node with more fields); didn't verify **shared field values** matched.

## Recovery Scripts
Two complementary scripts address distinct failure modes:

| Script                | Purpose | Key Features |
|-----------------------|---------|--------------|
| **`drupal_dedup_patch.py`** | **Patches existing nodes** with mismatched/missing fields (trusts XLSX as source). Skips equivalents; logs missing nodes. | - Normalizes fields (e.g., sorted sets for `field_sites_used`, YYYY-MM-DD dates, fuzzy lists).<br>- Logs: "MISSING" (empty in Drupal), "CONFLICT" (differs; both trigger PATCH).<br>- `--dry-run` previews changes. |
| **`drupal_recovery.py`** | **Re-creates completely missing nodes** from merged XLSX (phase 1 overrides phase 2 on collisions). | - Merges both import sets.<br>- Exact em-dash parsing.<br>- Reuses importer utils; tracks in `recovery_progress.json`.<br>- Safe to re-run; `--dry-run` previews. |

## Recommended Fix Sequence
1. **Patch existing nodes**:
   ```
   python drupal_dedup_patch.py --dry-run  # Review CONFLICT/MISSING
   python drupal_dedup_patch.py             # Live patch
   ```
   - Check summary: e.g., "Not in Drupal (missing): 12" → proceed if >0.

2. **Recover missing nodes** (only if needed):
   ```
   python drupal_recovery.py --dry-run
   python drupal_recovery.py                # Live; idempotent
   ```

## Prevention & Notes
- **Don't panic-run scripts**.
- Review `--dry-run` outputs; manually edited Drupal nodes may be overwritten.
- Original dedup didn't handle field value diffs—new scripts do via normalization.

<br>
