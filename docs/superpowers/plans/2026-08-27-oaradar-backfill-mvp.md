# OARadar Real OA Backfill MVP Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Produce a reconciled candidate Markdown build and CSV/JSON reports for 100 representative real OA items, then reuse the same command for all 6,144 targets.

**Architecture:** Add one narrow `backfill_mvp` module that selects a deterministic sample, invokes the existing metadata classifier and parse cache, renders isolated candidate Packages, and writes reports. Extend the existing classification run request only enough to freeze a selected target subset, and extend the parse-cache request only enough to distinguish classification parsing from candidate-Markdown conversion. No WebUI, Qwen, publication ledger, or formal pointer is added.

**Tech Stack:** Python 3.12, SQLAlchemy 2, Alembic, Pydantic 2, Typer, pytest, MarkItDown, local filesystem CSV/JSON/Markdown.

**Spec:** `docs/superpowers/specs/2026-08-27-oaradar-backfill-mvp-design.md`

## Global Constraints

- Never modify, move, delete, or overwrite anything below `data/originals/`.
- Excluded OA items are never classified, parsed, or rendered.
- Candidate output is confined to a new `data/markdown/.builds/<run_id>/` directory.
- A single-item failure is reported and the batch continues; only an unsafe output root or unreconciled final ledger fails the batch.
- Selection is a small deterministic function over existing fields, not a framework or database subsystem.
- The first real run selects exactly 100 target OA items, normally 60–70 ordinary and 30–40 special/boundary items.
- Progress is reported as real OA, Package, attachment conversion, review, failure, and reconciliation counts.

---

### Task 1: Select 100 targets and support a scoped classification run

**Files:**
- Create: `src/oa_knowledge/backfill_mvp.py`
- Modify: `src/oa_knowledge/classification/service.py`
- Create: `tests/test_backfill_mvp.py`
- Modify: `tests/test_classification_service.py`

**Interfaces:**
- Produces: `select_representative_items(session: Session, config: PrivateClassificationConfig, sample_size: int) -> tuple[SampleItem, ...]`
- Produces: `CreateClassificationRun.target_keys: tuple[str, ...] | None`
- Consumes: `collect_metadata_evidence()` and `classify_from_metadata()` through `ClassificationService`.

- [ ] **Step 1: Write failing selector tests**

Create synthetic manifest/OA/file rows covering excluded, ordinary, internal-template, external-document-number, transfer, no-number, multi-attachment, no-attachment, mixed-initiator, and abnormal-file cases. Assert exactly the requested target count, no excluded key, no duplicate key, at least 60% ordinary when enough ordinary rows exist, special coverage when available, and identical output after insertion-order changes.

- [ ] **Step 2: Run selector tests and confirm failure**

Run: `uv run pytest tests/test_backfill_mvp.py -q --no-cov`

Expected: import failure because `oa_knowledge.backfill_mvp` does not exist.

- [ ] **Step 3: Implement the deterministic selector in one module**

Add immutable `SampleItem(oa_item_key: str, bucket: str, reason: str)`. Compute special flags only from manifest status, title, document number, initiator role, attachment count/status, and confirmed-no-attachment evidence. Take at most 35 special rows by stable bucket priority and OA key, then fill the remainder from ordinary rows using evenly spaced indexes over the stable OA-key order. Do not add tables, configuration sections, or generic sampling abstractions.

- [ ] **Step 4: Add target scoping to existing classification runs**

Add `target_keys: tuple[str, ...] | None = None` to `CreateClassificationRun`. When set, freeze all excluded rows plus only those non-excluded keys; reject unknown, excluded-only, or duplicate requested keys; set `target_count` to the selected target count. When absent, preserve the current full-run behavior exactly.

- [ ] **Step 5: Verify selector and classification service**

Run: `uv run pytest tests/test_backfill_mvp.py tests/test_classification_service.py tests/test_classification_reporting.py -q --no-cov`

- [ ] **Step 6: Commit**

```bash
git add src/oa_knowledge/backfill_mvp.py src/oa_knowledge/classification/service.py tests/test_backfill_mvp.py tests/test_classification_service.py
git commit -m "feat: select scoped OA backfill samples"
```

### Task 2: Render isolated candidate Packages and reuse parsed content

**Files:**
- Modify: `src/oa_knowledge/backfill_mvp.py`
- Modify: `src/oa_knowledge/classification/parse_cache.py`
- Modify: `tests/test_backfill_mvp.py`
- Modify: `tests/test_classification_parse_cache.py`

**Interfaces:**
- Produces: `BackfillMVPService.run(request: BackfillMVPRequest) -> BackfillMVPResult`
- Produces: `ParseRequest.purpose: Literal["classification", "candidate_markdown"]`
- Produces: one `_index.md` per selected target and zero or more faithful attachment Markdown files.

- [ ] **Step 1: Write failing vertical-slice tests**

Using only synthetic originals, assert: excluded rows invoke neither classifier nor parser; a classified OA creates one Package; a `needs_review` OA also creates one Package below candidate `needs_review`; confirmed-no-attachment creates only `_index.md`; duplicate attachment SHA calls the parser once but materializes links/files in both Packages; invalid, changed, unsupported, and parser-failed attachments append exceptions and do not stop later OA items.

- [ ] **Step 2: Confirm vertical-slice tests fail**

Run: `uv run pytest tests/test_backfill_mvp.py tests/test_classification_parse_cache.py -q --no-cov`

- [ ] **Step 3: Permit parse-cache reuse for candidate Markdown without weakening metadata-first classification**

Add `purpose="classification"` to `ParseRequest`. Preserve the current early `not_required` result when `purpose == "classification" and metadata_unresolved is false`. Allow `purpose == "candidate_markdown"` after the same integrity, depth, source-path, SHA, parser-identity, and concurrency checks. Reject every unknown purpose.

- [ ] **Step 4: Implement Package rendering and per-item continuation**

In `BackfillMVPService`, create a fresh staging directory below `data/markdown/.builds/`, process the scoped classification run, and render each selected target. Use internal category, external canonical issuer, or candidate `needs_review` for routing; sanitize path components with the existing canonical helper. Write `_index.md` with OA metadata, classification, integrity, transfer chain, attachment list, and explicit warnings. For verified supported attachments, obtain/reuse a `ParseArtifact`, read its cache product, and write it unchanged beneath a small provenance frontmatter. Direct UTF-8 text files may be copied through deterministic decoding; unsupported or failed sources receive no fabricated content.

- [ ] **Step 5: Make reruns safe**

Reject an existing non-empty run directory unless its `build_manifest.json` has the same run ID and input hash. Build through a sibling temporary directory and rename it into place only after report reconciliation. Never write `current`, `internal`, or `external` at the markdown root.

- [ ] **Step 6: Verify candidate behavior**

Run: `uv run pytest tests/test_backfill_mvp.py tests/test_classification_parse_cache.py tests/test_classification_service.py -q --no-cov`

- [ ] **Step 7: Commit**

```bash
git add src/oa_knowledge/backfill_mvp.py src/oa_knowledge/classification/parse_cache.py tests/test_backfill_mvp.py tests/test_classification_parse_cache.py
git commit -m "feat: build isolated OA backfill candidates"
```

### Task 3: Add the batch CLI and fail-closed reconciliation reports

**Files:**
- Modify: `src/oa_knowledge/backfill_mvp.py`
- Modify: `src/oa_knowledge/cli.py`
- Modify: `tests/test_backfill_mvp.py`
- Modify: `tests/test_cli.py`
- Create: `docs/runbooks/oaradar-backfill-mvp.md`

**Interfaces:**
- Produces CLI: `oa backfill-mvp --run-id <id> --sample-size 100 --config <path>`
- Produces CLI full mode: `oa backfill-mvp --run-id <id> --all-targets --config <path>`
- Produces: `sample.csv`, `classification.csv`, `exceptions.csv`, `build_manifest.json`.

- [ ] **Step 1: Write failing report and CLI tests**

Assert CSV files use UTF-8 BOM and stable columns; reports contain no attachment body text; `build_manifest.json` contains manifest/excluded/target/selected/processed, classified/review, Package, attempted/converted/failed/skipped attachment counts, exception-code counts, and every generated file hash. Assert `selected == packages == classified + needs_review` and `attempted == converted + failed + skipped`. Assert the command exits nonzero on reconciliation failure and prints only the aggregate result/run path.

- [ ] **Step 2: Confirm report/CLI tests fail**

Run: `uv run pytest tests/test_backfill_mvp.py tests/test_cli.py -q --no-cov`

- [ ] **Step 3: Implement reports and CLI wiring**

Load the existing private classification config fail-closed, compute deterministic manifest/policy/rule/parser input hashes, run the service, validate the two equations plus unique OA Package ownership and every file SHA, then atomically publish the candidate run directory. Add `--sample-size` range 1–6144 and a mutually exclusive `--all-targets`; do not add WebUI or background-job machinery.

- [ ] **Step 4: Write the operational runbook**

Document database backup, migration, private-rule location/mode, dry candidate root, command invocation, progress metrics, exception interpretation, rerun behavior, and the explicit prohibition on formal publication.

- [ ] **Step 5: Run the complete MVP suite and repository safety checks**

Run:

```bash
uv run pytest tests/test_backfill_mvp.py tests/test_classification_service.py tests/test_classification_reporting.py tests/test_classification_parse_cache.py tests/test_cli.py -q --no-cov
uv run ruff check src/oa_knowledge/backfill_mvp.py src/oa_knowledge/classification/service.py src/oa_knowledge/classification/parse_cache.py tests/test_backfill_mvp.py
git status --short
```

- [ ] **Step 6: Commit**

```bash
git add src/oa_knowledge/backfill_mvp.py src/oa_knowledge/cli.py tests/test_backfill_mvp.py tests/test_cli.py docs/runbooks/oaradar-backfill-mvp.md
git commit -m "feat: operate and reconcile OA backfill MVP"
```

### Task 4: Run and verify the real 100-item candidate

**Files:**
- No tracked OA data or reports.
- Local-only outputs: configured database backup, `private/classification/`, and `data/markdown/.builds/<run_id>/`.

**Interfaces:**
- Consumes: the Task 3 CLI.
- Produces: the real 100-item candidate and its aggregate verification result.

- [ ] **Step 1: Preflight without printing confidential values**

Confirm the database is at revision `0037`, record aggregate `8119 = 1975 + 6144`, verify the originals root is unchanged, confirm free disk space, and inspect private config availability and permissions. If private rules are absent, create the four local-only YAML files from the already supplied role/document-number guidance; declare every other observed initiator `unknown` rather than guessing.

- [ ] **Step 2: Back up the database and migrate the working copy**

Create a timestamped SQLite backup below the configured state root, verify `PRAGMA integrity_check` on the backup, then upgrade the production database to the branch head. Do not alter OA records or originals.

- [ ] **Step 3: Execute the real 100-item run without a sample-confirmation pause**

Run:

```bash
uv run oa backfill-mvp --config /data/Projects/OARadar/config.yaml --run-id backfill-mvp-100-20260827 --sample-size 100
```

Allow individual item/attachment errors to accumulate in `exceptions.csv`; stop only for unsafe roots, migration/config failure, or unreconciled totals.

- [ ] **Step 4: Verify actual files and immutable originals**

Recompute every candidate file SHA, count Package `_index.md` files, verify every selected OA appears once, verify attachment equations, and compare a before/after originals inventory hash. Run the CLI a second time only in verification/resume mode and require identical manifest input/output hashes.

- [ ] **Step 5: Report business progress**

Report exactly: processed OA/100, Package count/100, attachment Markdown count, automatically classified count, `needs_review` count, failed/exception count, reconciliation result, candidate path, database backup path, and whether originals remained unchanged. Do not report code-task completion as the primary result.
