# Field Mapping Audit

Comparison of `field_names.csv` against `field_mapping.yaml` to verify completeness.

All 22 Drupal fields from `field_names.csv` are accounted for in `field_mapping.yaml`.

| CSV drupal_field | YAML section |
|---|---|
| field_ai_application | scanner_output_fields |
| field_ai_notes | scanner_output_fields |
| field_confidence | scanner_output_fields |
| field_reason | scanner_output_fields |
| field_risk_review_required | scanner_output_fields |
| field_sra | manually_entered_drupal |
| field_sites_used | sites_used |
| field_business_criticality_level | spreadsheet_to_drupal |
| field_business_sponsor_name_phon | spreadsheet_to_drupal |
| field_certificate_expiration_dat | spreadsheet_to_drupal |
| field_contains_phi | spreadsheet_to_drupal |
| field_contract_terms | spreadsheet_to_drupal |
| field_description | spreadsheet_to_drupal |
| field_division | spreadsheet_to_drupal |
| field_it_director_manager | spreadsheet_to_drupal |
| field_it_technical_contact | spreadsheet_to_drupal |
| field_mission_critical | spreadsheet_to_drupal |
| field_priority_for_business_cont | spreadsheet_to_drupal |
| field_product_name | spreadsheet_to_drupal |
| field_responsible_dept_category | spreadsheet_to_drupal |
| field_status | spreadsheet_to_drupal |
| field_vendor_name | spreadsheet_to_drupal |

Nothing is missing — no updates were needed.

## Discrepancy: SHH and SOM

The CSV's `allowed_values` for `field_sites_used` (and `field_division`) include `shh` and `som`, but the YAML's `sites_used.spreadsheet_columns` only lists 8 codes (no SHH or SOM). This matches the excel_field header in the CSV, so those two codes may exist in Drupal as valid values but have no corresponding spreadsheet column. Worth confirming whether SHH/SOM rows will ever be populated.
