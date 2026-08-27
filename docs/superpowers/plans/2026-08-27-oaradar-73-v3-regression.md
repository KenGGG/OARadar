# OARadar 73-Item V3 Regression Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Repair legacy `.doc` parsing, duplicate attachment materialization, internal business-subject classification, and Qwen fallback diagnostics; then rebuild and verify the existing 73-item regression corpus in a new candidate directory.

**Architecture:** Preserve every collected original and archive record.  Canonicalize only eligible duplicate attachment *content* for parsing and candidate Package materialization, retaining alias provenance in reports.  Extend the existing parse cache with a bounded `markitdown → antiword` legacy-DOC route, strengthen deterministic subject rules before local Qwen fallback, and report Qwen rejection reasons without relaxing its schema gate.

**Tech Stack:** Python 3.12, SQLAlchemy 2, pytest, MarkItDown, antiword, local MinerU, local Qwen, CSV/JSON/Markdown.

**Spec:** User acceptance requirements in this conversation; base candidate design: `docs/superpowers/specs/2026-08-27-oaradar-backfill-mvp-design.md`.

## Global Constraints

- Work only on `feature/oa-classification-phase1`; do not merge `main` or create a PR.
- Never modify, move, delete, or overwrite anything below `data/originals/`.
- Archive rows retain every discovered source key and original path; only ContentObject/ParseArtifact/Package views canonicalize duplicate content.
- A parser may use one primary engine and at most one fallback.
- `99_其他内部` is never the default category; unresolved evidence remains `needs_review`.
- Candidate `packages/needs_review/` is debug-only and remains outside formal MarkdownBuildService publication.
- Use synthetic or irreversibly redacted fixtures only in tests.

---

### Task 1: Canonical duplicate-content view

**Files:**
- Modify: `src/oa_knowledge/backfill_mvp.py`
- Modify: `src/oa_knowledge/collector/detail.py`
- Modify: `tests/test_backfill_mvp.py`
- Modify: `tests/test_detail_archive.py`

- [ ] Write failing synthetic tests proving that same-container, same-SHA, full-name/truncated-name `official_attachment`/`direct_attachment` aliases create one attachment Markdown, one index link, and an auditable duplicate count; prove unrelated same-content files and `official_body` are not collapsed.
- [ ] Run the focused tests and confirm the duplicate Package assertion fails under the current per-row loop.
- [ ] Add one narrow canonical-selection helper shared by candidate processing; select the full readable name and retain source file IDs/keys as aliases in build metadata.  Add collection-time suppression only for the same safe alias pattern after content is known.
- [ ] Run focused tests and verify the attachment/reconciliation equations include raw-source and canonical-content counts.

### Task 2: Legacy DOC parser route

**Files:**
- Create: `src/oa_knowledge/parsers/antiword_parser.py`
- Modify: `src/oa_knowledge/parsers/router.py`
- Modify: `src/oa_knowledge/classification/parse_cache.py`
- Modify: `src/oa_knowledge/backfill_mvp.py`
- Modify: `tests/test_parsers.py`
- Modify: `tests/test_backfill_mvp.py`

- [ ] Install the local `antiword` package and record its binary version.
- [ ] Write failing tests proving legacy `.doc` attempts MarkItDown then antiword once, stores actual engine/version and fallback reason, and fails on empty/low-quality antiword output.
- [ ] Implement an antiword adapter and cache identity; give `.doc` a `markitdown → antiword` route while keeping every other Office route unchanged.
- [ ] Bump the candidate parser profile/config identity and run parser/backfill tests.

### Task 3: Deterministic business-subject rules

**Files:**
- Modify: `src/oa_knowledge/classification/internal_classification.py`
- Modify: `tests/test_internal_classification.py`
- Modify: `tests/test_backfill_mvp.py`

- [ ] Write failing table-driven tests for rent-after/project transaction markers, external reporting, business-department briefing, and form/document-type separation.
- [ ] Replace raw hit counting with ordered, subject-level rule groups so project subject beats incidental risk/finance vocabulary, while explicit governance/funding/reporting evidence remains distinguishable.
- [ ] Run classification tests and inspect the 73-item decision delta before calling Qwen.

### Task 4: Qwen fallback diagnostics and strict repair

**Files:**
- Modify: `src/oa_knowledge/classification/internal_classification.py`
- Modify: `src/oa_knowledge/backfill_mvp.py`
- Modify: `tests/test_internal_classification.py`
- Modify: `tests/test_backfill_mvp.py`

- [ ] Write failing tests for `<think>`-wrapped JSON, missing evidence, illegal category, low confidence, and successful strict structured output.
- [ ] Add rejection taxonomy/reporting and a bounded extractor that accepts only a valid schema object after removing a leading reasoning block; do not change confidence/schema requirements.
- [ ] Run synthetic redacted Qwen-response tests and report real historical rejection counts without disclosing OA content.

### Task 5: 73-item candidate rebuild and verification

**Files:**
- No tracked real OA output or report.
- Candidate: `data/markdown/.builds/backfill-internal-73-v3-verified-20260827/`

- [ ] Snapshot originals path/size/SHA/mtime/ctime metadata without printing content.
- [ ] Run all targeted tests, then execute the 73 keys through a new run ID.
- [ ] Verify every candidate file hash, Package ownership, raw/canonical attachment equations, original metadata immutability, and idempotent resume.
- [ ] Generate aggregate local-only report: old/new classified comparison, review resolution/remainder reasons, 99 titles/count, parser and exception extension distributions, duplicate/antiword/Qwen metrics.
- [ ] Request an independent review before any full 6,144-item run or formal publication.
