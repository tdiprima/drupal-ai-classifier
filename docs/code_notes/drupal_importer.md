# drupal_importer.py

`drupal_importer.py` is a one-time Excel-to-Drupal import script that reads a software inventory workbook, merges duplicate product rows, and creates Drupal nodes through JSON:API.

Its main job is to turn spreadsheet data into Drupal content without creating duplicate nodes for the same product, while also surviving interrupted runs through a progress file.

## What It Does

For each unique product identified by `vendor + product`:

1. Reads the Excel workbook with `openpyxl`.
2. Skips metadata sheets that do not contain importable software rows.
3. Maps spreadsheet columns to Drupal field machine names.
4. Merges duplicate rows across sheets into a single record.
5. Normalizes constrained values to the exact keys Drupal expects.
6. Builds a JSON:API payload for the target Drupal content type.
7. POSTs the node to Drupal.
8. Records success in a progress file so reruns can resume cleanly.

It supports:

- dry-run previews
- resumable imports
- retrying transient POST failures
- partial imports with `--limit`
- resetting progress with `--reset`

## How It Works

### 1. Loads configuration

The script reads required settings from `.env`:

- `DRUPAL_BASE_URL`
- `DRUPAL_USERNAME`
- `DRUPAL_PASSWORD`
- `DRUPAL_CONTENT_TYPE`

It exits immediately if any are missing.

These values control:

- which Drupal instance to connect to
- which account to authenticate with
- which node bundle to create

### 2. Reads the workbook and skips non-data sheets

The importer opens the workbook with `openpyxl.load_workbook(..., data_only=True)`.

It ignores sheets that are treated as metadata or references:

- `MASTER Spreadsheet`
- `Priority Level Definitions`
- `Dropdowns`
- `Sheet1`

Only the remaining sheets are considered potential data sources for software records.

### 3. Detects required columns

Within each sheet, the importer looks for:

- a vendor column whose header contains both `vendor` and `name`
- a `product name` column
- mapped business fields from the configured Excel-to-Drupal field map
- site columns such as `SBUH`, `SBSH`, `SBELIH`, `CPMP`, `SBAS`, `HSC`, `MHL`, and `SDM`

If a sheet does not contain vendor and product columns, the entire sheet is skipped.

### 4. Filters out incomplete rows

A row is importable only if it has all three of these values:

- vendor
- product
- description

If any of those are missing, the row is skipped and logged with the missing field names.

This is important because the importer treats description as required input, not optional metadata.

### 5. Merges duplicate products across sheets

The uniqueness key is:

```text
(vendor.lower(), product.lower())
```

If the same product appears more than once:

- `field_sites_used` is unioned across all matching rows
- for other fields, the first non-empty value wins
- later conflicting non-empty values are ignored and logged as conflicts

That means the importer is not doing last-write-wins and it is not producing multiple Drupal nodes for the same product. It collapses repeated spreadsheet rows into a single Drupal record.

### 6. Maps Excel columns to Drupal fields

The script converts spreadsheet labels into Drupal machine names before building the payload.

Examples:

- `vendor name` -> `field_vendor_name`
- `product name` -> `field_product_name`
- `description` -> `field_description`
- `business criticality level` -> `field_business_criticality_level`
- `certificate expiration date` -> `field_certificate_expiration_dat`

It also builds `field_sites_used` from `X` marks in the site columns instead of from a single spreadsheet cell.

### 7. Normalizes values before sending them to Drupal

The importer does not blindly POST raw spreadsheet values. It applies field-specific normalization first.

#### List and select fields

For constrained fields such as:

- `field_status`
- `field_division`
- `field_contains_phi`
- `field_mission_critical`
- `field_business_criticality_level`
- `field_priority_for_business_cont`
- `field_sites_used`

the importer tries to match the spreadsheet value to an allowed Drupal key.

It does that in this order:

1. exact match
2. case-insensitive match
3. fuzzy match using `difflib.get_close_matches()`

If a value still does not match, the field is skipped rather than sending an invalid choice.

#### Integer list fields

`field_priority_for_business_cont` is treated as an integer list field.

Values like `5` and `5.0` are coerced to integers before validation.

#### Date fields

`field_certificate_expiration_dat` is normalized to `YYYY-MM-DD`.

If the value cannot be recognized in that format, the field is skipped.

#### Multi-value site field

`field_sites_used` is converted into the JSON:API object-array structure Drupal expects:

```json
[{"value": "sbuh"}, {"value": "sbsh"}]
```

Invalid site names are dropped with a warning.

### 8. Builds a Drupal JSON:API payload

Each merged record is transformed into:

```json
{
  "data": {
    "type": "node--<content_type>",
    "attributes": {
      "title": "Vendor Name — Product Name",
      "...": "..."
    }
  }
}
```

The node title is always derived from:

```text
Vendor Name — Product Name
```

That title becomes the human-readable identity of the created node.

### 9. Posts nodes with retry logic

The importer sends each payload to:

```text
/jsonapi/node/<content_type>
```

using an authenticated `requests.Session`.

Retry behavior is intentionally limited and pragmatic:

- transient connection errors are retried
- timeouts are retried
- server-side HTTP errors are retried up to `MAX_RETRIES`
- most client-side `4xx` errors are not retried

There is one special case for `HTTP 422`.

If Drupal rejects the payload because one or more list-field values are invalid, the importer:

1. parses the error response
2. identifies the rejected fields
3. removes only those fields from the payload
4. retries the POST immediately

This allows a node to be created even if one bad select/list value would otherwise block the entire record.

### 10. Tracks progress so imports can resume

The script stores completed records in a JSON progress file.

By default that file is:

```text
../import_progress.json
```

The progress file records:

- completed `(vendor, product)` keys
- last updated timestamp
- counts for succeeded, failed, and skipped records
- timing information for the last run

On later runs:

- records already present in the progress file are skipped
- failed records are attempted again
- interrupted runs continue where they left off

### 11. Supports dry runs and reset behavior

With `--dry-run`, the script does not contact Drupal. It only logs which nodes would be created.

With `--reset`, it deletes the progress file before starting, which forces the import to run from scratch.

## Usage

```bash
python drupal_importer.py --dry-run
python drupal_importer.py
python drupal_importer.py --limit 10
python drupal_importer.py --reset
python drupal_importer.py --spreadsheet /path/to/file.xlsx
python drupal_importer.py --progress-file /path/to/progress.json
```

### Arguments

- `--dry-run`: preview records without posting to Drupal
- `--debug`: enable verbose logging
- `--reset`: delete the progress file and restart from the beginning
- `--limit N`: process only the first `N` merged records
- `--spreadsheet PATH`: override the default workbook path
- `--progress-file PATH`: override the default progress file path

## Default Paths

- Spreadsheet: `~/Documents/misc/software_inventory.xlsx`
- Progress file: `../import_progress.json`

## What The Import Summary Means

At the end of a live run, the script logs:

- total merged records found in the spreadsheet
- records skipped because they were already completed earlier
- records imported successfully in the current run
- records that failed in the current run
- records still remaining
- elapsed time
- progress file location

If there are failures, the intended recovery path is simply to rerun the script.

## Important Behavioral Details

This importer is optimized for practical one-time migration, not perfect reconciliation.

Important consequences:

- duplicate product rows are collapsed into one node
- first non-empty conflicting field values win
- invalid individual fields can be dropped while the rest of the node still imports
- successful records are checkpointed immediately
- SSL verification is disabled for the current internal Drupal environment

That last point is explicitly acceptable for the current internal server setup in the script, but it should not be carried forward unchanged into a public-facing deployment.
