# Key Project Facts

Quick-reference for project configuration, ports, URLs, and other stuff
you always forget.

> **NEVER put secrets, passwords, API keys, or tokens in this file.**
> Use `.env` files (gitignored), cloud secret managers, or CI/CD variables instead.

## Project Info

- **Project name:** drupal-excel-importer
- **Language/Framework:** Python 3.11+ / Drupal JSON:API
- **Key dependencies:** openai, openpyxl, python-dotenv, requests
- **Virtual env:** `.venv/` (local)

## Environments

- **Drupal base URL:** set via `DRUPAL_BASE_URL` env var
- **Auth:** Basic HTTP auth — `DRUPAL_USERNAME` / `DRUPAL_PASSWORD`
- **Content type:** set via `DRUPAL_CONTENT_TYPE` (e.g., `software_inventory`, `new_product`)
- **API endpoint pattern:** `{DRUPAL_BASE_URL}/jsonapi/node/{content_type}`
- **SSL:** verification currently disabled for internal environment
- **Node title format:** `"Vendor Name — Product Name"` (em-dash separator)

## Key Files

| File | Purpose |
|------|---------|
| `src/drupal_importer.py` | Main ETL — reads Excel, merges dupes, POSTs to Drupal |
| `src/drupal_ai_scanner.py` | Post-import AI enrichment via Azure OpenAI |
| `src/audit_reporter.py` | Diagnostic audit — compares expected vs. actual Drupal fields |
| `src/drupal_patcher.py` | PATCHes incorrect/missing fields on existing nodes |
| `src/patch/patch_division.py` | Division field patching |
| `src/patch/patch_criticality.py` | Business criticality patching |
| `src/utility/drupal_dedup.py` | Deduplication helper |
| `field_mapping.yaml` | Excel column → Drupal field mapping config (source of truth) |
| `import_progress.json` | Auto-created resumable checkpoint file |
| `.env.example` | Template for required environment variables |

## Required Environment Variables

```
DRUPAL_BASE_URL       # e.g., https://your-drupal.example.com
DRUPAL_USERNAME       # service account username
DRUPAL_PASSWORD       # service account password
DRUPAL_CONTENT_TYPE   # Drupal content type machine name

# Only needed for AI scanner:
OPENAI_API_KEY
AZURE_OPENAI_ENDPOINT
AZURE_OPENAI_API_KEY
AZURE_OPENAI_DEPLOYMENT

# Optional:
LOG_LEVEL             # defaults to INFO
```

## Other Facts

- Progress file (`import_progress.json`) tracks completed nodes with timestamps — delete to restart from scratch
- Site codes union across duplicate rows (8 site columns, "X" marks)
- Duplicate rows (same vendor + product) are merged before import — first non-empty value wins for conflicts
- AI scanner runs separately after import; it PATCHes 4 fields: `field_ai_application`, `field_confidence`, `field_reason`, `field_risk_review_required`
- HTTP 422 errors: invalid field values are dropped and node is retried without that field
