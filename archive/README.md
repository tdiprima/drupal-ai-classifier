# outgoing-witchblade 🧙‍♀️

A Python ETL pipeline that syncs software inventory data from Excel spreadsheets into a Drupal CMS via JSON:API.

## The Spreadsheet-to-CMS Gap

Managing institutional software inventory in spreadsheets works fine until you need that data searchable, auditable, and accessible through a content management system. Manually re-entering hundreds of software products — with vendor details, PHI flags, mission-critical designations, multi-site usage, and AI-assisted security assessments — is error-prone and unsustainable.

## What This Does

This project reads a structured Excel workbook (`software_inventory.xlsx`) and maps each row to a Drupal `new_product` content node via the JSON:API. A YAML-based field mapping configuration bridges spreadsheet column names to Drupal field machine names, making the mapping explicit and easy to audit. The pipeline supports 22 fields per product, covering everything from vendor and product name to AI scanner classifications and security risk assessment status.

## Example

A spreadsheet row for a clinical application:

| Vendor Name | Product Name | PHI | Mission Critical | Priority |
|---|---|---|---|---|
| Acme Corp | PatientTracker Pro | Yes | Yes | High |

...maps via `field_mapping.yaml` to a Drupal node with fields like `field_vendor_name`, `field_product_name`, `field_phi`, `field_mission_critical`, and `field_priority`, then POSTed to the Drupal JSON:API endpoint.

## Usage

**Prerequisites:**

- Python 3.10+
- A Drupal instance with JSON:API enabled and a `new_product` content type
- Your Excel inventory file at `~/Documents/misc/software_inventory.xlsx`

**Setup:**

```bash
# Install dependencies
pip install requests openpyxl python-dotenv

# Copy and fill in credentials
cp .env.example .env
```

`.env` fields:

```
DRUPAL_BASE_URL=https://your-drupal-site.example.com
DRUPAL_USER=your_username
DRUPAL_PASS=your_password
CONTENT_TYPE=new_product
```

**Run:**

```bash
# Query existing product nodes from Drupal
python src/query.py

# Read and preview spreadsheet data with column header mapping
python src/read_spreadsheet.py

# Post a test article node to verify connectivity
python src/hello_post.py
```

**Field mapping** is defined in `field_mapping.yaml`. Each entry maps a spreadsheet column header to a Drupal field machine name. Edit this file to adapt the pipeline to a different spreadsheet structure or content type.

The `field_names.csv` file documents all 22 Drupal fields, their types, and allowed values — useful when auditing field configuration or onboarding new contributors.

**Authentication:** The pipeline currently uses HTTP Basic Auth. See `docs/auth.md` for the OAuth2 setup guide recommended for production deployments.

<br>
