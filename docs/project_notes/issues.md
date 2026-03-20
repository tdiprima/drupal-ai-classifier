# Work Log

## Template

### YYYY-MM-DD - TICKET-XXX: Short Description

**What was done**: Summary of work completed

**Related files**: List of key files changed

**Notes**: Anything useful for future reference

---

## Entries

(Add new entries below this line, newest first)

---

### 2026-03-20 - SETUP: Initialize Project Memory System

**What was done**: Set up `docs/project_notes/` memory system. Populated `key_facts.md` with project config, environment variables, and key file index. Populated `decisions.md` with 4 ADRs derived from code analysis. CLAUDE.md already had memory-aware protocols.

**Related files**: `docs/project_notes/key_facts.md`, `docs/project_notes/decisions.md`, `docs/project_notes/bugs.md`, `docs/project_notes/issues.md`, `CLAUDE.md`

**Notes**: Memory files were scaffolded previously but empty. ADRs 001-004 capture decisions visible from code structure as of this date.

---

### 2026-03-20 - REFACTOR: Reorganize Utilities & Reuse Importer

**What was done**: Reorganized utility scripts into `src/utility/` and `src/patch/` subdirectories. Refactored importer for reuse.

**Related files**: `src/utility/`, `src/patch/`, `src/drupal_importer.py`

**Notes**: Commit d7bca9c. Followed by addition of SHH/SOM sites and patch tool (f25bd15), division patch script (a4ca6a2), and node dedup script (05f9bbb).

---
