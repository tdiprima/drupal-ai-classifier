# Architectural Decision Records

## Template

### ADR-XXX: Decision Title

**Date**: YYYY-MM-DD

**Status**: Active | Superseded by ADR-XXX

**Context**: Why this decision was needed

**Decision**: What was decided

**Alternatives**: What else was considered

**Consequences**: What this means going forward

---

## Entries

(Add new decisions below this line, newest first)

---

### ADR-004: AI Enrichment as Separate Post-Import Step

**Date**: 2026-03-20

**Status**: Active

**Context**: AI classification of software entries (AI usage, confidence, risk) is expensive and not needed for the base import.

**Decision**: Run `drupal_ai_scanner.py` separately after initial import. It fetches existing nodes and PATCHes AI fields back.

**Alternatives**: Classify during import (slower, harder to retry); batch classify before import (requires two-pass Excel read).

**Consequences**: Import and AI enrichment are independently resumable. AI step can be re-run without re-importing.

---

### ADR-003: Deferred Field Normalization Before POST

**Date**: 2026-03-20

**Status**: Active

**Context**: Drupal JSON:API returns HTTP 422 for invalid field values. Needed a strategy for handling bad data.

**Decision**: Normalize all field values (list matching, integer coercion, date parsing) before POST. On 422 error, drop the offending field and retry the node without it.

**Alternatives**: Reject entire row on any bad field; post-process and PATCH bad fields later.

**Consequences**: Nodes import even with partial data. Bad fields are logged but don't block import. Requires audit step to catch gaps.

---

### ADR-002: Spreadsheet-First Source of Truth via field_mapping.yaml

**Date**: 2026-03-20

**Status**: Active

**Context**: Multiple Drupal content types and evolving field names made hardcoding mappings fragile.

**Decision**: `field_mapping.yaml` defines all Excel-column-to-Drupal-field mappings, field types, and site columns. Scripts read this config rather than hardcoding.

**Alternatives**: Hardcode mappings per script; use Drupal's field config API at runtime.

**Consequences**: Changing a field mapping only requires editing YAML, not code. New content types can be supported by adding a YAML section.

---

### ADR-001: Merge-on-Duplicate with Union of Site Codes

**Date**: 2026-03-20

**Status**: Active

**Context**: Same vendor+product pair appears across multiple Excel sheets (one per site). Importing each row would create duplicate Drupal nodes.

**Decision**: Before import, merge rows with identical vendor+product into a single record. Site codes are unioned (all "X" marks collected). For other fields, first non-empty value wins.

**Alternatives**: Import all rows and deduplicate in Drupal later; use a separate dedup script post-import.

**Consequences**: One Drupal node per unique vendor+product. `field_sites_used` reflects all sites using that product. `drupal_dedup.py` exists for cleanup if duplicates slip through.

---
