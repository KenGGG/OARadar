# Feature Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Classify the frozen 6,144-item historical target set with metadata-first escalation and expose every decision, exception, and CSV export in 已办资料 without publishing Markdown.

**Architecture:** A new versioned classification ledger is authoritative; `oa_items` remains only a compatibility projection. `ClassificationService` freezes targets, computes per-item fingerprints, evaluates package-scoped metadata, parses only unresolved attachments through a versioned cache, and calls a localhost-only Qwen adapter last. Runs and item checkpoints are durable, manual locks are immutable to automation, and WebUI reads only completed decision versions.

**Tech Stack:** Python 3.12, SQLAlchemy 2, Alembic, Pydantic 2, FastAPI, Typer, React 19, TypeScript 5.8, pytest, local Ollama/Qwen.

**Spec:** `docs/superpowers/specs/2026-08-26-oa-markdown-v1-classification-design.md`

## Global Constraints

- Never commit real OA values, private YAML, databases, logs, parsed text, or generated Dry Run reports.
- Keep `data/` limited to `originals/` and `markdown/`; classification state belongs in the configured database and private rules under ignored `private/classification/`.
- Treat originals as read-only; all OA identifiers remain text and all archive paths remain relative to `data_root`.
- Stop container traversal at depth 10; unvisited children force `content_integrity_status=missing` and publication blocking.
- Metadata runs first. Parsing and Qwen are forbidden when metadata already resolves the Package.
- Attachment-scoped evidence may describe an attachment, but may not change an already established internal Package to external.
- Automated runs may append a superseding decision but may never overwrite or unlock a manual locked decision.
- No task in this plan writes formal Markdown or changes `data/markdown/current`.

---

### Task 1: Add migration 0038 and authoritative classification models

**Files:**
- Create: `src/oa_knowledge/db/migrations/versions/0038_oa_markdown_v1_classification.py`
- Modify: `src/oa_knowledge/db/models.py`
- Create: `tests/test_classification_migration.py`
- Modify: `tests/test_database.py`

- [ ] **Step 1: Write failing migration tests**

Assert upgrade from `0037_no_attachment_evidence` creates `classification_runs`, `classification_run_items`, `classification_decisions`, and `classification_evidence`; adds `profile_version` to `parse_artifacts`; and creates the versioned parse reuse unique index. Assert all OA keys and foreign keys use text-safe identities and downgrade restores the exact 0037 schema.

- [ ] **Step 2: Run the focused tests and confirm failure**

Run: `uv run pytest tests/test_classification_migration.py tests/test_database.py -q --no-cov`

Expected: failure because revision 0038 and model classes do not exist.

- [ ] **Step 3: Implement the schema and model constraints**

Add models `ClassificationRun`, `ClassificationRunItem`, `ClassificationDecision`, and `ClassificationEvidence`. Required invariants:

- `classification_runs`: unique `run_id`, immutable version/config hashes, target/excluded counts, lifecycle `created|running|completed|failed`, timestamps, summary JSON.
- `classification_run_items`: unique `(run_id, oa_item_key)`, frozen inclusion reason, stage `queued|metadata|parse|content|qwen|decided|failed`, attempts and error.
- `classification_decisions`: version number unique per OA key; exactly one current row per OA key through a partial unique index; `decision_input_sha256`; `decision_source`; `classification_status`; `content_integrity_status`; `content_origin`; `flow_type`; `initiator_type`; `relay_from`; `transfer_chain_json`; raw `issuer`; `canonical_issuer`; `business_category`; `document_number`; `document_type`; `normalized_title`; confidence/reason; rule/private-config versions; lock/actor metadata; and `supersedes_decision_id`.
- `classification_evidence`: stable sequence unique per decision, type, value JSON, confidence, and `evidence_scope` constrained to `package|attachment`.
- `parse_artifacts.profile_version` is non-null after backfill to `legacy`; reuse identity is unique over `(content_object_id, engine, engine_version, profile_version, config_hash)`.

Use check constraints for all closed vocabularies from the spec. External decisions reject non-empty `business_category`; classified decisions require internal or external origin; excluded decisions cannot be publishable.

- [ ] **Step 4: Run migration/model tests**

Run: `uv run pytest tests/test_classification_migration.py tests/test_database.py -q --no-cov`

Expected: pass.

- [ ] **Step 5: Commit**

```bash
git add src/oa_knowledge/db/migrations/versions/0038_oa_markdown_v1_classification.py src/oa_knowledge/db/models.py tests/test_classification_migration.py tests/test_database.py
git commit -m "feat: add versioned OA classification ledger"
```

### Task 2: Load private classification configuration safely

**Files:**
- Create: `src/oa_knowledge/classification/__init__.py`
- Create: `src/oa_knowledge/classification/schemas.py`
- Create: `src/oa_knowledge/classification/private_config.py`
- Modify: `src/oa_knowledge/config.py`
- Modify: `.gitignore`
- Create: `examples/classification/initiator_profiles.example.yaml`
- Create: `examples/classification/document_number_issuers.example.yaml`
- Create: `examples/classification/issuer_aliases.example.yaml`
- Create: `examples/classification/title_templates.example.yaml`
- Create: `tests/test_classification_private_config.py`

- [ ] **Step 1: Write failing loader and privacy tests**

Cover strict schema validation, four required YAML files, duplicate aliases, an alias pointing to two canonical issuers, missing initiator role, unknown role reporting, resolved paths below the configured private root, and POSIX mode broader than `0600`. Test only synthetic organizations and people.

- [ ] **Step 2: Confirm the tests fail**

Run: `uv run pytest tests/test_classification_private_config.py tests/test_config.py -q --no-cov`

Expected: import or validation failures.

- [ ] **Step 3: Implement the configuration contract**

Expose:

```python
class PrivateClassificationConfig(BaseModel):
    initiators: dict[str, InitiatorProfile]
    document_number_issuers: list[DocumentNumberIssuerRule]
    issuer_aliases: dict[str, str]
    title_templates: list[TitleTemplateRule]

def load_private_classification_config(root: Path) -> LoadedPrivateConfig: ...
```

`LoadedPrivateConfig` includes `config` and a deterministic `config_sha256`. Add only `OA_CLASSIFICATION_PRIVATE_DIR` to settings; `.env` stores the directory path, never names or rules. Fail closed on missing/unsafe files. Public examples must be synthetic and document the five initiator roles: `internal`, `external`, `mixed`, `system`, `unknown`.

- [ ] **Step 4: Add ignore rules and prove secrets stay untracked**

Ignore `private/classification/` while retaining public examples. Run:

```bash
git check-ignore private/classification/issuer_aliases.yaml
git ls-files private data
```

Expected: the private path is ignored and neither private configuration nor runtime data is tracked.

- [ ] **Step 5: Run tests and commit**

Run: `uv run pytest tests/test_classification_private_config.py tests/test_config.py -q --no-cov`

```bash
git add .gitignore src/oa_knowledge/classification src/oa_knowledge/config.py examples/classification tests/test_classification_private_config.py
git commit -m "feat: load private OA classification rules"
```

### Task 3: Separate archive readiness from classification and fingerprint each OA

**Files:**
- Create: `src/oa_knowledge/classification/readiness.py`
- Create: `src/oa_knowledge/classification/fingerprint.py`
- Create: `tests/test_classification_readiness.py`
- Create: `tests/test_classification_fingerprint.py`

- [ ] **Step 1: Write failing readiness tests**

Cover `ok`, `no_attachment_confirmed`, `missing`, `size_mismatch`, `sha256_mismatch`, `download_failed`, and `not_checked`. A confirmed zero-attachment item is publishable; every other non-`ok` value except `no_attachment_confirmed` blocks publication but does not imply a classification error.

- [ ] **Step 2: Write failing fingerprint tests**

Verify `decision_input_sha256` is stable across DB row order and changes when—and only when—one of these inputs changes: normalized OA metadata, manifest/exclusion state, attachment inventory/hashes/relationships, transfer evidence, used ParseArtifact cache keys, manual-decision version, rule/schema/prompt versions, or private config hash.

- [ ] **Step 3: Confirm failures**

Run: `uv run pytest tests/test_classification_readiness.py tests/test_classification_fingerprint.py -q --no-cov`

- [ ] **Step 4: Implement explicit services**

Expose:

```python
class ArchiveReadinessService:
    def assess(self, session: Session, oa_item_key: str) -> IntegrityAssessment: ...

def decision_input_sha256(inputs: DecisionInputs) -> str: ...
```

Canonicalize JSON with sorted keys, normalized timestamps, text OA IDs, ordered file relationships, relative paths, and lowercase hashes. Never read or mutate file content merely to classify if the stored verified hash and size are current.

- [ ] **Step 5: Run tests and commit**

Run: `uv run pytest tests/test_classification_readiness.py tests/test_classification_fingerprint.py -q --no-cov`

```bash
git add src/oa_knowledge/classification/readiness.py src/oa_knowledge/classification/fingerprint.py tests/test_classification_readiness.py tests/test_classification_fingerprint.py
git commit -m "feat: separate OA integrity and decision fingerprints"
```

### Task 4: Implement scoped metadata evidence and deterministic rules

**Files:**
- Create: `src/oa_knowledge/classification/evidence.py`
- Create: `src/oa_knowledge/classification/metadata_rules.py`
- Create: `tests/test_classification_metadata_rules.py`
- Create: `tests/fixtures/classification/metadata_cases.yaml`

- [ ] **Step 1: Write a synthetic decision table before implementation**

Include title templates, initiator roles, document-number issuer mappings, direct and multi-level transfers, alias normalization, conflicting issuer aliases, system/mixed/unknown initiators, and internal workflows containing an external formal attachment. Every fixture declares expected evidence scope, origin, category/issuer, confidence, decision source, and escalation action.

- [ ] **Step 2: Confirm the rule tests fail**

Run: `uv run pytest tests/test_classification_metadata_rules.py -q --no-cov`

- [ ] **Step 3: Implement normalized evidence and priority within scope**

Expose:

```python
def collect_metadata_evidence(item: ClassificationItem, config: PrivateClassificationConfig) -> list[Evidence]: ...
def classify_from_metadata(evidence: Sequence[Evidence]) -> RuleOutcome: ...
def normalize_issuer(raw: str, aliases: Mapping[str, str]) -> IssuerResolution: ...
def build_transfer_chain(item: ClassificationItem, evidence: Sequence[Evidence]) -> list[str]: ...
```

Rules compare priority only within `package` or `attachment` scope. Persist `relay_from` as the final predecessor convenience value and `transfer_chain` as the full ordered list. Ambiguous aliases, unknown initiators, and equally strong conflicting package evidence yield unresolved/needs review—not a default origin. `business_category` is populated only for internal outcomes; external outcomes require canonical issuer.

- [ ] **Step 4: Add the non-overreach regression**

Test an internal approval-form Package with an attachment bearing an external formal document number. Expected: `content_origin=internal`; attachment evidence retains its external issuer and `attachment` scope.

- [ ] **Step 5: Run tests and commit**

Run: `uv run pytest tests/test_classification_metadata_rules.py -q --no-cov`

```bash
git add src/oa_knowledge/classification/evidence.py src/oa_knowledge/classification/metadata_rules.py tests/test_classification_metadata_rules.py tests/fixtures/classification/metadata_cases.yaml
git commit -m "feat: classify OA packages from scoped metadata"
```

### Task 5: Add durable runs, decision versioning, and manual locks

**Files:**
- Create: `src/oa_knowledge/classification/service.py`
- Create: `src/oa_knowledge/classification/reporting.py`
- Create: `tests/test_classification_service.py`
- Create: `tests/test_classification_reporting.py`

- [ ] **Step 1: Write failing orchestration tests**

Test frozen membership, exactly one terminal state per target, resume after failure, unchanged fingerprint reuse, changed single-item recomputation, atomic current-decision swap, and idempotent reruns. Prove an automatic run cannot supersede a locked manual decision; a new manual action may create a later locked version with actor, reason, and timestamp.

- [ ] **Step 2: Write the baseline reconciliation test**

Using synthetic scaled fixtures, enforce:

```text
total = excluded + publishable + integrity_blocked + needs_review
classification_target = publishable + integrity_blocked + needs_review
```

Report configured initiators grouped into all five roles and list unknown separately.
The report also includes needs-parse count, actual parse count, expected/actual Qwen calls, conflicts, unrecognized issuers, and canonical-document deduplication count.

- [ ] **Step 3: Confirm failures**

Run: `uv run pytest tests/test_classification_service.py tests/test_classification_reporting.py -q --no-cov`

- [ ] **Step 4: Implement the public service boundary**

Expose:

```python
class ClassificationService:
    def create_run(self, request: CreateClassificationRun) -> ClassificationRunRef: ...
    def process_next(self, run_id: str, *, limit: int = 100) -> Progress: ...
    def resume(self, run_id: str) -> Progress: ...
    def set_manual_decision(self, command: ManualDecisionCommand) -> DecisionView: ...
    def complete(self, run_id: str) -> ClassificationRunReport: ...
```

Freeze manifest membership and exclusions at run creation. Item transactions are short and restartable. Do not mirror incomplete decisions to `oa_items`; compatibility fields update only after the new decision is committed current. Manual lock conflicts return a domain error, never silent success.

- [ ] **Step 5: Run tests and commit**

Run: `uv run pytest tests/test_classification_service.py tests/test_classification_reporting.py -q --no-cov`

```bash
git add src/oa_knowledge/classification/service.py src/oa_knowledge/classification/reporting.py tests/test_classification_service.py tests/test_classification_reporting.py
git commit -m "feat: orchestrate versioned OA classification runs"
```

### Task 6: Make ParseArtifact reuse version-aware and strictly on-demand

**Files:**
- Create: `src/oa_knowledge/classification/parse_cache.py`
- Modify: `src/oa_knowledge/parsers/router.py`
- Modify: `src/oa_knowledge/parsers/markitdown_parser.py`
- Modify: `src/oa_knowledge/parsers/mineru_parser.py`
- Create: `tests/test_classification_parse_cache.py`
- Modify: `tests/test_parsers.py`

- [ ] **Step 1: Write failing cache-key and escalation tests**

Prove reuse occurs only for the same `(content_sha256, parser_name, parser_version, parse_profile_version, parse_config_sha256)`. Changing parser or profile creates a new artifact. Two attachments with one content object reuse the same artifact. Metadata-resolved items invoke neither router nor parser.

- [ ] **Step 2: Confirm failures**

Run: `uv run pytest tests/test_classification_parse_cache.py tests/test_parsers.py -q --no-cov`

- [ ] **Step 3: Implement the adapter**

Expose:

```python
class ParseCacheService:
    def get_or_parse(self, request: ParseRequest) -> ParseArtifactRef: ...
```

Use the content object SHA as content identity and the DB unique key as the concurrency arbiter. Write parse output only to the existing local derived cache path, atomically; a uniqueness race re-reads and returns the winner. Record only artifacts actually consulted in `DecisionInputs`.

- [ ] **Step 4: Enforce depth and integrity behavior**

If a required nested child is beyond depth 10 or missing, return an integrity block and enqueue `depth_limit_reached` where applicable. Do not mark the OA complete and do not send incomplete parsed content to Qwen.

- [ ] **Step 5: Run tests and commit**

Run: `uv run pytest tests/test_classification_parse_cache.py tests/test_parsers.py -q --no-cov`

```bash
git add src/oa_knowledge/classification/parse_cache.py src/oa_knowledge/parsers tests/test_classification_parse_cache.py tests/test_parsers.py
git commit -m "feat: add version-aware on-demand parse cache"
```

### Task 7: Add content rules and a localhost-only Qwen fallback

**Files:**
- Create: `src/oa_knowledge/classification/content_rules.py`
- Create: `src/oa_knowledge/classification/qwen.py`
- Modify: `src/oa_knowledge/classification/service.py`
- Modify: `src/oa_knowledge/config.py`
- Create: `tests/test_classification_content_rules.py`
- Create: `tests/test_classification_qwen.py`

- [ ] **Step 1: Write failing escalation-order tests**

Assert call order `metadata -> selected parse -> content rules -> Qwen`; assert each resolved stage prevents later calls. Test bounded input snippets, structured JSON output validation, timeout, malformed output, model unavailable, low confidence, and network URL rejection.

- [ ] **Step 2: Confirm failures**

Run: `uv run pytest tests/test_classification_content_rules.py tests/test_classification_qwen.py tests/test_classification_service.py -q --no-cov`

- [ ] **Step 3: Implement content classification**

Content rules consume only selected parsed artifacts and produce scoped evidence. They may use the first relevant pages and filenames but must preserve the Package/attachment scope boundary. Resolve only when deterministic evidence clears the configured confidence threshold.

- [ ] **Step 4: Implement the Qwen client guardrail**

Expose:

```python
class LocalQwenClassifier:
    def classify(self, request: QwenClassificationRequest) -> QwenOutcome: ...
```

Allow only `http://127.0.0.1` or `http://localhost`; reject redirects and every other host/scheme. Send minimum necessary title/metadata/snippets, require schema-valid evidence and confidence, and use `decision_source=local_qwen`. Any failure becomes `needs_review`, never an invented default.

- [ ] **Step 5: Run tests and commit**

Run: `uv run pytest tests/test_classification_content_rules.py tests/test_classification_qwen.py tests/test_classification_service.py -q --no-cov`

```bash
git add src/oa_knowledge/classification src/oa_knowledge/config.py tests/test_classification_content_rules.py tests/test_classification_qwen.py tests/test_classification_service.py
git commit -m "feat: add content and local Qwen classification fallback"
```

### Task 8: Expose review, filters, CSV, and manual decisions through the API

**Files:**
- Create: `src/oa_knowledge/web/classification_views.py`
- Modify: `src/oa_knowledge/web/app.py`
- Modify: `src/oa_knowledge/web/console_views.py`
- Create: `tests/test_classification_views.py`
- Modify: `tests/test_console_views.py`
- Modify: `tests/test_web_security.py`

- [ ] **Step 1: Write failing API contract tests**

Specify:

- `GET /api/classification/runs/latest`
- `GET /api/classification/items`
- `GET /api/classification/items/export.csv`
- `GET /api/classification/items/{oa_item_key}`
- `POST /api/classification/items/{oa_item_key}/manual-decision`

Filters include query, content origin, business category, canonical issuer, decision source, classification status, integrity status, initiator role, confidence range, manual lock, and run ID. CSV must apply the identical filter object and deterministic ordering; no-filter export contains the whole selected run.

- [ ] **Step 2: Add authorization and privacy assertions**

Mutation endpoints require CSRF and local authorization. Responses expose decision evidence but not private rule-file paths, Qwen raw prompts, parsed full text, or secrets. CSV cells beginning with `=`, `+`, `-`, or `@` are neutralized against spreadsheet formula injection.

- [ ] **Step 3: Confirm failures**

Run: `uv run pytest tests/test_classification_views.py tests/test_console_views.py tests/test_web_security.py -q --no-cov`

- [ ] **Step 4: Implement query DTO reuse**

Create one `ClassificationFilters` model consumed by list and export paths. Manual requests require origin/category-or-issuer/reason and always create a new locked immutable decision. A correction is another authenticated manual decision that supersedes the prior version; no API may unlock a decision for automation.

- [ ] **Step 5: Run tests and commit**

Run: `uv run pytest tests/test_classification_views.py tests/test_console_views.py tests/test_web_security.py -q --no-cov`

```bash
git add src/oa_knowledge/web/classification_views.py src/oa_knowledge/web/app.py src/oa_knowledge/web/console_views.py tests/test_classification_views.py tests/test_console_views.py tests/test_web_security.py
git commit -m "feat: expose OA classification review API"
```

### Task 9: Integrate classification review into 已办资料

**Files:**
- Modify: `webui/src/views/SimpleDoneView.tsx`
- Modify: `webui/src/types/simple-status.ts`
- Modify: `webui/src/App.tsx`
- Modify: `webui/src/styles.css`
- Create: `webui/src/components/ClassificationReviewDrawer.tsx`
- Create: `tests/test_frontend_classification_assets.py`

- [ ] **Step 1: Write failing frontend asset contract tests**

Assert source contains the classification filters, unknown/integrity warning counters, evidence scope labels, transfer chain, CSV export preserving current query, and manual lock controls. Do not test compiled/minified artifacts.

- [ ] **Step 2: Confirm failure and type-check baseline**

Run:

```bash
uv run pytest tests/test_frontend_classification_assets.py -q --no-cov
cd webui && npm run check
```

- [ ] **Step 3: Implement the existing-page workflow**

Keep users in 已办资料. Add compact chips for origin/status/integrity/source; advanced filters; filtered/unfiltered CSV export; a details drawer with ordered package and attachment evidence; multi-level transfer chain; raw/canonical issuer; decision fingerprint; and manual decision form. Clearly distinguish “分类待复核” from “原件不完整”.

- [ ] **Step 4: Verify accessibility and build**

Use labeled native controls, keyboard-operable drawer, focus return, status text in addition to color, and confirmation before one manual decision supersedes another. Run:

```bash
cd webui && npm run check && npm run build
cd .. && uv run pytest tests/test_frontend_classification_assets.py -q --no-cov
```

- [ ] **Step 5: Commit source only**

```bash
git add webui/src tests/test_frontend_classification_assets.py
git commit -m "feat: review OA classifications in done archives"
```

### Task 10: Add durable worker/CLI execution and pass the historical Dry Run Gate

**Files:**
- Modify: `src/oa_knowledge/cli.py`
- Modify: `src/oa_knowledge/web/worker.py`
- Create: `tests/test_classification_cli.py`
- Modify: `tests/test_worker.py`
- Modify: `tests/test_runtime_paths.py`
- Create: `docs/runbooks/oa-markdown-v1-dry-run.md`

- [ ] **Step 1: Write failing command and worker tests**

Specify `oa classification create-run`, `oa classification resume`, `oa classification report`, and `oa classification export`. Test singleton claiming, bounded batches, heartbeat, retry, graceful restart, no formal Markdown writes, and a worker-disabled mode suitable for manual Dry Run.

- [ ] **Step 2: Confirm failures**

Run: `uv run pytest tests/test_classification_cli.py tests/test_worker.py tests/test_runtime_paths.py -q --no-cov`

- [ ] **Step 3: Implement and document the operational flow**

The runbook must include private file permission checks, DB backup, migration, creation, resume, WebUI review, CSV export, rollback of the migration before production use, and explicit commands proving `data/` has only two top-level directories.

Before the first run, reconcile the observed legacy `data/runtime/` tree with the configured external `state_root/runtime/`: enumerate exact files without following symlinks, hash-copy each missing file with restrictive permissions, verify source/destination hashes, and abort on any name/content conflict. Only after every file is verified, move the legacy tree as one recoverable directory into an operator-selected backup below `state_root`; do not delete it automatically. Add a regression that all new runtime reports resolve outside `data_root`.

- [ ] **Step 4: Run the complete automated Phase 1 suite**

Run:

```bash
uv run pytest tests/test_classification_migration.py tests/test_classification_private_config.py tests/test_classification_readiness.py tests/test_classification_fingerprint.py tests/test_classification_metadata_rules.py tests/test_classification_service.py tests/test_classification_reporting.py tests/test_classification_parse_cache.py tests/test_classification_content_rules.py tests/test_classification_qwen.py tests/test_classification_views.py tests/test_classification_cli.py tests/test_console_views.py tests/test_worker.py tests/test_runtime_paths.py tests/test_web_security.py tests/test_frontend_classification_assets.py -q --no-cov
cd webui && npm run check && npm run build
```

Expected: all pass.

- [ ] **Step 5: Run the local historical Dry Run without publishing**

Use the runbook against a DB backup. Gate evidence must report:

```text
manifest total          8119
excluded                1975
classification targets  6144
target terminal states  6144
```

Also prove all 40 configured initiator identifiers are accounted for, unknown is separately listed, metadata-resolved items have zero parse/Qwen calls, all decisions have fingerprints, every target appears exactly once, and `data/markdown/current` is unchanged. Keep the report local and untracked.

- [ ] **Step 6: Obtain user review before Phase 2**

Show the completed Dry Run in 已办资料 and export the same filtered results to CSV. Do not start the Markdown candidate plan until the user approves the classification output.

- [ ] **Step 7: Commit**

```bash
git add src/oa_knowledge/cli.py src/oa_knowledge/web/worker.py tests/test_classification_cli.py tests/test_worker.py tests/test_runtime_paths.py docs/runbooks/oa-markdown-v1-dry-run.md
git commit -m "feat: operate OA classification dry runs"
```

## Phase 1 Exit Gate

- [ ] Historical counts reconcile exactly: `8119 = 1975 + publishable + integrity_blocked + needs_review`, and the latter three total 6,144.
- [ ] Every target has one current decision, one integrity state, and a `decision_input_sha256`.
- [ ] Unknown initiators and ambiguous issuers are explicit review queues.
- [ ] Manual decisions remain locked after an automated rerun.
- [ ] Parse and Qwen audit counts prove metadata-first escalation.
- [ ] Filtered and unfiltered CSV exports match the visible run/filter counts.
- [ ] No formal Markdown path or original file changed.
- [ ] User explicitly approves the Dry Run before Phase 2 begins.
