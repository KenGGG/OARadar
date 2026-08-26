# Feature Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a complete, immutable OA Markdown V1 candidate from an approved classification run and prove one-to-one reconciliation without changing the formal published view.

**Architecture:** `MarkdownBuildService` freezes current approved decisions into a build ledger, renders each publishable OA into one deterministic Package below `data/markdown/.builds/<run_id>/`, and validates counts, paths, hashes, links, frontmatter, and integrity. Builds never reclassify, never parse opportunistically, and never touch `current`, `internal`, or `external`.

**Tech Stack:** Python 3.12, SQLAlchemy 2, Alembic, Pydantic 2, pathlib, PyYAML, Typer, FastAPI, pytest.

**Spec:** `docs/superpowers/specs/2026-08-26-oa-markdown-v1-classification-design.md`

## Global Constraints

- Entry requires explicit approval of the Phase 1 Dry Run.
- The build consumes a frozen classification run and exact ParseArtifact identities; it does not rerun rules or Qwen.
- Only `classification_status=classified` with integrity `ok|no_attachment_confirmed` is publishable.
- `no_attachment_confirmed` creates `_index.md` only.
- External Packages have empty `business_category` and live below normalized `canonical_issuer`; internal Packages use `business_category`.
- Attachment origin/issuer metadata stays attachment-scoped and never relocates the Package.
- Write only below `data/markdown/.builds/`; never modify originals or the formal published view.

---

### Task 1: Add migration 0039 and the Markdown build ledger

**Files:**
- Create: `src/oa_knowledge/db/migrations/versions/0039_oa_markdown_v1_builds.py`
- Modify: `src/oa_knowledge/db/models.py`
- Create: `tests/test_markdown_v1_migration.py`
- Modify: `tests/test_database.py`

- [ ] **Step 1: Write failing migration tests**

Assert `markdown_builds` and `markdown_build_items` are created from 0038 and removed on downgrade. Require unique build ID, frozen classification run, input signature, relative build root, lifecycle, expected/actual state counts, QA result/hash, and timestamps. Require unique `(build_id, oa_item_key)` and `(build_id, package_relpath)`.

- [ ] **Step 2: Confirm failure**

Run: `uv run pytest tests/test_markdown_v1_migration.py tests/test_database.py -q --no-cov`

- [ ] **Step 3: Implement models and constraints**

Add `MarkdownBuild` and `MarkdownBuildItem`. Item status is `queued|rendered|validated|failed`; store frozen decision ID/fingerprint, package relative path, `_index.md` SHA-256, file count, and error. All paths are relative to `data_root`; the ledger must not name a live/current path.

- [ ] **Step 4: Run tests and commit**

Run: `uv run pytest tests/test_markdown_v1_migration.py tests/test_database.py -q --no-cov`

```bash
git add src/oa_knowledge/db/migrations/versions/0039_oa_markdown_v1_builds.py src/oa_knowledge/db/models.py tests/test_markdown_v1_migration.py tests/test_database.py
git commit -m "feat: add OA markdown candidate build ledger"
```

### Task 2: Generate safe, stable, collision-proof Package paths

**Files:**
- Create: `src/oa_knowledge/markdown_v1/__init__.py`
- Create: `src/oa_knowledge/markdown_v1/paths.py`
- Create: `tests/test_markdown_v1_paths.py`

- [ ] **Step 1: Write failing path tests**

Cover duplicate dates/titles, duplicate forwards, punctuation/control characters, reserved names, slashes, whitespace, emoji/CJK truncation, 120-byte title limit, 160-byte total component limit, and deterministic normalization on Linux. Assert no `(1)`/`(2)` suffix is ever generated.

- [ ] **Step 2: Confirm failure**

Run: `uv run pytest tests/test_markdown_v1_paths.py -q --no-cov`

- [ ] **Step 3: Implement the exact naming rule**

Expose:

```python
def oa_short_id(oa_item_key: str) -> str:
    # first 12 lowercase hex of SHA256(UTF-8 oa_item_key)

def package_component(date_value: date, title: str, oa_item_key: str) -> str: ...
def package_relpath(decision: FrozenDecision) -> PurePosixPath: ...
```

The component is exactly `YYYYMMDD-<safe-title>--oa_<short-id>`. Layout is `internal/<business_category>/YYYY/MM/<component>/` or `external/<canonical_issuer>/YYYY/MM/<component>/`. Truncate without splitting a Unicode code point, validate every resolved destination remains under its build root, and keep the full title only in `_index.md`.

- [ ] **Step 4: Run tests and commit**

Run: `uv run pytest tests/test_markdown_v1_paths.py -q --no-cov`

```bash
git add src/oa_knowledge/markdown_v1 tests/test_markdown_v1_paths.py
git commit -m "feat: generate stable OA package paths"
```

### Task 3: Render canonical Package indexes and attachment Markdown

**Files:**
- Create: `src/oa_knowledge/markdown_v1/render.py`
- Create: `tests/test_markdown_v1_render.py`
- Create: `tests/fixtures/markdown_v1/internal_package.json`
- Create: `tests/fixtures/markdown_v1/external_package.json`
- Create: `tests/fixtures/markdown_v1/no_attachment_package.json`

- [ ] **Step 1: Write failing golden tests using synthetic data**

Assert deterministic YAML/frontmatter order and all required fields: `oa_item_key`, complete `title`, `normalized_title`, `content_origin`, `flow_type`, `initiator`, `initiator_type`, `relay_from`, `transfer_chain`, raw/canonical issuer, `document_number`, `document_type`, internal-only `business_category`, `canonical_document_ids`, `source_oa_ids`, `decision_source`, classification reason/confidence/decision ID, decision fingerprint, `content_integrity_status`, OA completion time, and Markdown generation time. Evidence summaries retain scopes. Test filenames and relative links.

- [ ] **Step 2: Add no-attachment and evidence-scope regressions**

For `no_attachment_confirmed`, assert the Package contains exactly `_index.md`. For an internal Package with an external attachment issuer, assert the index remains internal while the attachment Markdown records `attachment_origin=external` and its issuer.

- [ ] **Step 3: Confirm failure**

Run: `uv run pytest tests/test_markdown_v1_render.py -q --no-cov`

- [ ] **Step 4: Implement pure render functions**

Expose:

```python
def render_package_index(package: PackageRenderModel) -> str: ...
def render_attachment(attachment: AttachmentRenderModel) -> str: ...
```

Render from frozen decisions and existing ParseArtifacts only. Generate `正文.md` only when a real body artifact exists and one attachment Markdown per selected attachment artifact; never invent either. Do not read private config or invoke classification. Escape YAML safely, normalize line endings, and prevent Markdown link traversal. Parsed output must be referenced by content identity and exact parser/profile versions.

- [ ] **Step 5: Run tests and commit**

Run: `uv run pytest tests/test_markdown_v1_render.py -q --no-cov`

```bash
git add src/oa_knowledge/markdown_v1/render.py tests/test_markdown_v1_render.py tests/fixtures/markdown_v1
git commit -m "feat: render OA markdown v1 packages"
```

### Task 4: Build immutable candidates from frozen decisions

**Files:**
- Create: `src/oa_knowledge/markdown_v1/build.py`
- Create: `tests/test_markdown_v1_build.py`

- [ ] **Step 1: Write failing build service tests**

Test build creation freezes decision IDs/fingerprints and state counts; only publishable rows become packages; excluded, needs-review, and integrity-blocked items remain ledger counts but get no directory. Test resume, idempotence, concurrent claim, atomic per-package temp-directory rename, and rejection if a frozen decision is later changed.

- [ ] **Step 2: Confirm failure**

Run: `uv run pytest tests/test_markdown_v1_build.py -q --no-cov`

- [ ] **Step 3: Implement the service boundary**

Expose:

```python
class MarkdownBuildService:
    def create(self, classification_run_id: str) -> MarkdownBuildRef: ...
    def process_next(self, build_id: str, *, limit: int = 50) -> BuildProgress: ...
    def resume(self, build_id: str) -> BuildProgress: ...
```

Create `data/markdown/.builds/<build_id>/` with restrictive permissions. Reject non-completed or unapproved classification runs. Never follow symlinks within the staging tree; never overwrite a completed immutable Package.

- [ ] **Step 4: Verify no prohibited side effects**

Snapshot originals and formal link targets before a test build, then assert identical hashes/targets afterward. Assert `find data -mindepth 1 -maxdepth 1 -type d` still returns only the two approved roots.

- [ ] **Step 5: Run tests and commit**

Run: `uv run pytest tests/test_markdown_v1_build.py -q --no-cov`

```bash
git add src/oa_knowledge/markdown_v1/build.py tests/test_markdown_v1_build.py
git commit -m "feat: build immutable OA markdown candidates"
```

### Task 5: Validate reconciliation, hashes, frontmatter, and links

**Files:**
- Create: `src/oa_knowledge/markdown_v1/validate.py`
- Create: `tests/test_markdown_v1_validate.py`
- Create: `tests/fixtures/markdown_v1/invalid_candidates/README.md`

- [ ] **Step 1: Write failing validator tests**

Inject duplicate OA keys, duplicate Package paths, missing/extra `_index.md`, wrong index hash, malformed YAML, escaped path, broken attachment link, external category leakage, missing canonical issuer, incorrect no-attachment content, mutated frozen decision, and an unexpected file.

- [ ] **Step 2: Confirm failure**

Run: `uv run pytest tests/test_markdown_v1_validate.py -q --no-cov`

- [ ] **Step 3: Implement a fail-closed validator**

Expose:

```python
class MarkdownBuildValidator:
    def validate(self, build_id: str) -> BuildValidationReport: ...
```

Required equations:

```text
total = excluded + publishable + integrity_blocked + needs_review
publishable = markdown_build_items = Package directories = _index.md files
each published oa_item_key appears exactly once
```

Walk without following symlinks, hash every generated file, parse every frontmatter block, resolve every local link, and compare every decision fingerprint with the frozen ledger. A failed QA status is immutable evidence; fixes require a new build.

- [ ] **Step 4: Run tests and commit**

Run: `uv run pytest tests/test_markdown_v1_validate.py -q --no-cov`

```bash
git add src/oa_knowledge/markdown_v1/validate.py tests/test_markdown_v1_validate.py tests/fixtures/markdown_v1/invalid_candidates/README.md
git commit -m "feat: validate OA markdown candidate builds"
```

### Task 6: Add candidate operations and WebUI QA visibility

**Files:**
- Modify: `src/oa_knowledge/cli.py`
- Create: `src/oa_knowledge/web/markdown_build_views.py`
- Modify: `src/oa_knowledge/web/app.py`
- Modify: `src/oa_knowledge/web/worker.py`
- Modify: `webui/src/App.tsx`
- Modify: `webui/src/types/simple-status.ts`
- Create: `webui/src/views/MarkdownBuildView.tsx`
- Create: `tests/test_markdown_v1_cli.py`
- Create: `tests/test_markdown_build_views.py`
- Modify: `tests/test_worker.py`
- Create: `tests/test_frontend_markdown_build_assets.py`
- Create: `docs/runbooks/oa-markdown-v1-build.md`

- [ ] **Step 1: Write failing API/CLI/worker tests**

Specify `oa markdown-v1 create-build`, `resume-build`, `validate-build`, and `build-report`; plus GET endpoints for builds, progress, state reconciliation, QA failures, and Package preview. Creating a candidate requires the approved run ID; no publish endpoint exists in this phase.

- [ ] **Step 2: Write failing UI contract tests**

The Markdown page shows frozen run/build IDs, four-way counts, progress, validation state, QA errors, no-attachment count, and read-only Package previews. It must state that the build is not formally published.

- [ ] **Step 3: Confirm failures**

Run: `uv run pytest tests/test_markdown_v1_cli.py tests/test_markdown_build_views.py tests/test_worker.py tests/test_frontend_markdown_build_assets.py -q --no-cov`

- [ ] **Step 4: Implement bounded execution and read-only review**

Use worker batches and heartbeats; keep validation separately triggerable and idempotent. The WebUI may create/continue a candidate only behind CSRF confirmation but cannot promote it.

- [ ] **Step 5: Verify Phase 2 suite**

Run:

```bash
uv run pytest tests/test_markdown_v1_migration.py tests/test_markdown_v1_paths.py tests/test_markdown_v1_render.py tests/test_markdown_v1_build.py tests/test_markdown_v1_validate.py tests/test_markdown_v1_cli.py tests/test_markdown_build_views.py tests/test_worker.py tests/test_frontend_markdown_build_assets.py -q --no-cov
cd webui && npm run check && npm run build
```

- [ ] **Step 6: Commit**

```bash
git add src/oa_knowledge/cli.py src/oa_knowledge/web src/oa_knowledge/markdown_v1 webui/src tests docs/runbooks/oa-markdown-v1-build.md
git commit -m "feat: operate and review OA markdown builds"
```

### Task 7: Produce and approve the first full candidate

**Files:**
- No tracked data files; use `docs/runbooks/oa-markdown-v1-build.md`

- [ ] **Step 1: Back up the database and create a build from the approved Dry Run**

Record build ID and frozen input signature locally. Do not add the resulting report or Package files to Git.

- [ ] **Step 2: Run validation twice**

The second run must return the same QA report hash and counts. Required result: prepared Package count equals publishable count; every publishable key corresponds to exactly one Package; blocked/review/excluded keys correspond to none.

- [ ] **Step 3: Review samples in WebUI**

Review internal, external, mixed-initiator, alias-normalized, multi-transfer, no-attachment, and attachment-scoped issuer examples. Confirm complete titles survive in `_index.md` even when directory titles are truncated.

- [ ] **Step 4: Obtain explicit user approval**

Do not begin Phase 3 promotion until the user approves this exact immutable build ID and QA report hash.

## Phase 2 Exit Gate

- [ ] The approved build is tied to one approved classification run and immutable decision fingerprints.
- [ ] All count equations and one-to-one Package invariants pass.
- [ ] Every generated hash, frontmatter block, and local link validates.
- [ ] `no_attachment_confirmed` Packages contain `_index.md` only.
- [ ] Formal `current`, `internal`, and `external` views are unchanged.
- [ ] User explicitly approves the exact build ID before Phase 3 begins.
