# Drupal Excel Importer

Software inventory lives in Excel, but Drupal needs clean JSON:API content. Manual entry is slow, inconsistent, and duplicates products across sheets.

This project reads the spreadsheet, merges duplicate vendor/product rows, collects site usage flags, maps columns to Drupal fields, and creates Drupal nodes through JSON:API.

## Example

If `Adobe + Acrobat` appears on multiple sheets, the importer combines those rows into one record, unions the site codes marked with `X`, and posts a single Drupal node like `Adobe — Acrobat`.

## Usage

Install dependencies:

```bash
uv sync
```

Create a `.env` file:

```env
DRUPAL_BASE_URL=https://your-drupal-site
DRUPAL_USERNAME=your-username
DRUPAL_PASSWORD=your-password
DRUPAL_CONTENT_TYPE=software_inventory
```

Preview the import:

```bash
python src/drupal_importer.py --dry-run --spreadsheet /path/to/software_inventory.xlsx
```

Run the live import:

```bash
python src/drupal_importer.py --spreadsheet /path/to/software_inventory.xlsx
```

<br>
