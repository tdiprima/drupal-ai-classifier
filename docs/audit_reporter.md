# audit_importer.py

`utility/audit_importer.py` is a diagnostic script that explains why fields from the Excel-to-Drupal import did not end up populated in Drupal.

Instead of importing data, it replays the importer's spreadsheet logic, fetches the matching Drupal node through JSON:API, compares expected values against stored values, and writes a CSV report showing the likely root cause for every field gap.

## What It Does

For each unique software product identified by `vendor + product`:

1. Reads the same Excel workbook used by `drupal_importer.py`.
2. Merges duplicate rows the same way the importer does.
3. Preserves the raw per-sheet values before merge so it can detect conflicts.
4. Fetches the corresponding Drupal node by title.
5. Compares every expected Drupal field with the actual value stored in Drupal.
6. Assigns a status and explanation for each field.
7. Writes all results to a CSV report and prints summary statistics.

The output is designed to answer questions like:

- Was the source cell empty in Excel?
- Was the Excel column missing from the sheet header?
- Did multiple sheets disagree on the value?
- Did the value fail the importer's normalization rules?
- Did the value look valid but still never land in Drupal?
- Was the node missing entirely in Drupal?

## How It Works

### 1. Loads configuration

The script reads the same Drupal connection settings as the importer from `.env`:

- `DRUPAL_BASE_URL`
- `DRUPAL_USERNAME`
- `DRUPAL_PASSWORD`
- `DRUPAL_CONTENT_TYPE`

It exits early if any required setting is missing.

### 2. Reuses the importer's field rules

The audit intentionally mirrors the importer's logic instead of inventing a second interpretation of the spreadsheet.

It carries over:

- The sheet skip list (`MASTER Spreadsheet`, `Dropdowns`, etc.)
- The site columns (`SBUH`, `SBSH`, `SBELIH`, `CPMP`, `SBAS`, `HSC`, `MHL`, `SDM`)
- The Excel-to-Drupal field map
- Allowed values for constrained Drupal fields
- Integer list field handling
- Date field normalization rules

This matters because the audit is trying to explain what the importer would have done, not what an idealized importer should have done.

### 3. Reads the workbook and captures raw values before merge

The script opens the workbook with `openpyxl` and processes every non-skipped sheet.

For each valid row:

- It requires `vendor`, `product`, and `description`, matching the importer's row acceptance rules.
- It builds a unique key from lowercase vendor and product.
- It records the raw cell value seen for each mapped field, along with the sheet name.
- It records whether the expected Excel column header was present at all.
- It records site usage from `X` marks in the site columns.

This raw tracking is what allows the report to distinguish between:

- empty source data
- missing headers
- conflicting values across sheets

### 4. Reconstructs the merged record exactly like the importer

After capturing raw values, the script merges rows using the same behavior as `drupal_importer.py`:

- `field_sites_used` is unioned across all matching rows
- the first non-empty value wins for regular fields
- later non-empty conflicting values are ignored for the merged record, but retained in the audit data

That gives the script two useful views of the data:

- the merged record the importer would have sent
- the raw pre-merge history needed to explain anomalies

### 5. Fetches the Drupal node over JSON:API

For each merged product, the script builds the title as:

```text
Vendor Name — Product Name
```

It then queries:

```text
/jsonapi/node/<content_type>
```

using `filter[title]`.

The response is reduced to the auditable fields plus metadata. If multiple nodes match, it logs a warning and uses the first one. If no node is found, every field for that record is reported as `Node not found in Drupal`.

### 6. Normalizes values for apples-to-apples comparison

Before deciding whether a field matches, the script applies the same transformation rules the importer used:

- list fields are validated against Drupal's allowed values
- matching is attempted exactly, case-insensitively, and then fuzzily
- integer list fields are coerced to integers
- date fields are reduced to `YYYY-MM-DD`
- multi-value site fields are normalized to sorted comma-separated strings

This prevents false positives where Drupal has the right normalized value but Excel had a different raw representation.

### 7. Classifies each field gap by likely root cause

Each auditable field gets a status and a human-readable explanation.

Statuses include:

- `Imported correctly`
- `Missing source data`
- `Mapping issue`
- `Conflict detected`
- `Import skipped`
- `Data transformation error`
- `Node not found in Drupal`

The classification logic is priority-based.

For normal fields, the script checks:

1. Did Drupal already store a value?
2. Was the source column missing from sheet headers?
3. Were all raw source cells empty?
4. Did different sheets provide conflicting non-empty values?
5. Did the surviving value fail normalization or allowed-value checks?
6. If the value looked valid, did something on the Drupal side still prevent it from being stored?

For `field_sites_used`, it applies similar logic but handles site sets as a multi-value field.

### 8. Writes a CSV report and summary

The output CSV contains one row per field per product with:

- `node_id`
- `drupal_field`
- `excel_column`
- `excel_value`
- `drupal_value`
- `status`
- `explanation`

It also logs summary totals by status and ranks the fields with the most issues.

## Usage

```bash
python utility/audit_importer.py --spreadsheet /path/to/file.xlsx
python utility/audit_importer.py --spreadsheet /path/to/file.xlsx --output report.csv
python utility/audit_importer.py --spreadsheet /path/to/file.xlsx --limit 10 --debug
```

### Arguments

- `--spreadsheet`: path to the Excel workbook to audit
- `--output`: destination CSV path, default `audit_report.csv`
- `--limit`: audit only the first N merged records
- `--debug`: enable verbose logging

## What The Report Is Good For

Use this script when the import completed but Drupal fields are unexpectedly blank or inconsistent.

It is most useful for:

- validating spreadsheet headers before another import run
- identifying bad source values that fail allowed-value checks
- finding conflicts hidden across multiple sheets
- separating Excel-side problems from Drupal-side storage problems
- producing a concrete remediation list before patching data

## Important Limitation

This script infers root causes from importer logic plus Drupal's stored state. It does not replay the original POST requests or inspect historical HTTP responses.

That means `Import skipped` is an informed diagnosis, not a guaranteed proof. In those cases, the value looked valid from the spreadsheet side, but the field is still empty in Drupal, which usually points to a Drupal-side rejection, a field mismatch, or another import-time issue outside the spreadsheet transform layer.
