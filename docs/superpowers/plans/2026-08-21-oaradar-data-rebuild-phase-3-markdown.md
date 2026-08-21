# OARadar Data Rebuild Phase 3 Markdown Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Regenerate the complete classified Markdown knowledge library from copied originals, including one `_index.md` per confirmed item and one independently named body Markdown only for items with document numbers.

**Architecture:** Select the body deterministically, parse copied originals into the rebuilt `parse/` tree, publish body/attachment Markdown into the confirmed directory, and record every output in `RebuildOutput`. The live Source Markdown tree and live export rows remain untouched until cutover.

**Tech Stack:** Python 3.12, SQLAlchemy 2, MarkItDown, MinerU, pytest, existing parser router and Markdown renderer.

**Spec:** `docs/superpowers/specs/2026-08-21-oaradar-data-cleanup-markdown-rebuild-design.md`

## Global Constraints

- Read only copied originals below `data_rebuilt/archive/`; never parse from OA or the live archive in this phase.
- Do not copy text from old Markdown into the rebuilt library.
- A document-number item requires exactly one body Markdown; a no-number item requires none.
- Selected body originals remain preserved but do not also publish ordinary attachment Markdown.
- Each supported file is parsed with the current router; unsupported files remain linked and explicitly listed.
- One file failure must not remove other successful outputs.
- Final paths require `classification_state='confirmed'` and an effective date.

---

### Task 1: Select body sources deterministically

**Files:**
- Create: `src/oa_knowledge/rebuild/body_source.py`
- Test: `tests/test_rebuild_body_source.py`

**Interfaces:**
- Produces: `BodySource(kind: Literal["attachment", "page_body", "none"], source_file_id: int | None, reason: str)`
- Produces: `select_body_source(item: OAItem, files: Sequence[ArchivedFile], page_body_available: bool) -> BodySource`
- Produces: `body_markdown_filename(item: OAItem) -> str | None`
- Produces: `load_verified_page_body(session: Session, settings: Settings, item_id: int) -> str | None`, reading only a verified local `body_snapshot` and sanitizing HTML to text.

- [ ] **Step 1: Write the body rules as tests**

```python
def test_no_document_number_has_no_body(item, files):
    item.document_number = None
    assert select_body_source(item, files, True).kind == "none"
    assert body_markdown_filename(item) is None

def test_official_body_role_wins(item, official_body, named_body):
    item.document_number = "示例〔2026〕12号"
    result = select_body_source(item, [named_body, official_body], True)
    assert result.source_file_id == official_body.id

def test_page_body_is_fallback(item):
    item.document_number = "示例〔2026〕12号"
    assert select_body_source(item, [], True).kind == "page_body"

def test_page_body_loader_rejects_unverified_snapshot(session, settings, body_snapshot):
    body_snapshot.download_status = "failed"
    assert load_verified_page_body(session, settings, body_snapshot.oa_item_id) is None
```

- [ ] **Step 2: Run tests and confirm failure**

Run: `uv run pytest tests/test_rebuild_body_source.py -v`

- [ ] **Step 3: Implement priority and safe filename rules**

Priority is `official_body` role, then a source filename matching 正文, full document number, or full title, then page body. Multiple matches use role priority, then smallest depth, then file ID for deterministic results.

- [ ] **Step 4: Run focused tests**

Run: `uv run pytest tests/test_rebuild_body_source.py -v`

- [ ] **Step 5: Commit**

```bash
git add src/oa_knowledge/rebuild/body_source.py tests/test_rebuild_body_source.py
git commit -m "feat: select numbered item body sources"
```

### Task 2: Parse copied originals into the rebuilt parse tree

**Files:**
- Create: `src/oa_knowledge/rebuild/parser.py`
- Modify: `src/oa_knowledge/parsers/router.py`
- Test: `tests/test_rebuild_parser.py`

**Interfaces:**
- Produces: `RebuildParseResult(source_file_id: int, status: str, engine: str, output_relpath: str | None, source_sha256: str, product_sha256: str | None, error_code: str | None)`
- Produces: `parse_rebuilt_source(session: Session, settings: Settings, run_id: int, source_file_id: int) -> RebuildParseResult`

- [ ] **Step 1: Write parser source-boundary tests**

```python
def test_parser_reads_rebuilt_original_not_live_file(session, settings, rebuilt_original):
    result = parse_rebuilt_source(session, settings, run_id=1, source_file_id=rebuilt_original.source_file_id)
    assert result.source_sha256 == rebuilt_original.sha256
    assert result.output_relpath.startswith("parse/")

def test_unsupported_is_explicit(session, settings, unsupported_original):
    result = parse_rebuilt_source(session, settings, 1, unsupported_original.source_file_id)
    assert result.status == "unsupported"
```

- [ ] **Step 2: Run tests and confirm failure**

Run: `uv run pytest tests/test_rebuild_parser.py -v`

- [ ] **Step 3: Implement parse isolation**

Resolve the source only from successful `RebuildOutput(kind='original')`. Write to a temporary directory below `data_rebuilt/parse/`, verify output hashes, atomically promote the successful directory, and retain only the latest successful result for the run.

- [ ] **Step 4: Run parser tests**

Run: `uv run pytest tests/test_rebuild_parser.py tests/test_parsers.py -v`

- [ ] **Step 5: Commit**

```bash
git add src/oa_knowledge/rebuild/parser.py src/oa_knowledge/parsers/router.py tests/test_rebuild_parser.py
git commit -m "feat: parse rebuilt originals in isolation"
```

### Task 3: Publish body and attachment Markdown

**Files:**
- Create: `src/oa_knowledge/rebuild/markdown.py`
- Modify: `src/oa_knowledge/markdown_export/render.py`
- Test: `tests/test_rebuild_markdown.py`

**Interfaces:**
- Produces: `publish_rebuilt_body(session: Session, settings: Settings, run_id: int, item_id: int) -> RebuildOutput | None`
- Produces: `publish_rebuilt_attachment(session: Session, settings: Settings, run_id: int, source_file_id: int) -> RebuildOutput`
- Consumes: `markdown_item_relpath`, `select_body_source`, and successful rebuilt parse output.

- [ ] **Step 1: Write body and attachment publishing tests**

```python
def test_numbered_item_gets_named_body(session, settings, numbered_item):
    output = publish_rebuilt_body(session, settings, 1, numbered_item.id)
    assert output.kind == "body_markdown"
    assert output.target_relpath.endswith("示例〔2026〕12号-示例事项-正文.md")

def test_selected_body_is_not_republished_as_attachment(session, settings, body_file):
    with pytest.raises(BodySourceDuplicateError):
        publish_rebuilt_attachment(session, settings, 1, body_file.id)
```

- [ ] **Step 2: Run tests and confirm failure**

Run: `uv run pytest tests/test_rebuild_markdown.py -v`

- [ ] **Step 3: Implement atomic Markdown publishing**

Render current frontmatter with title, OA text ID, document number, effective date, source type, internal category, external issuer, source SHA256, and parser version. Write through a same-directory temporary file, fsync, hash, and atomically replace only when the target belongs to this rebuild run.

- [ ] **Step 4: Run focused tests**

Run: `uv run pytest tests/test_rebuild_markdown.py tests/test_source_markdown_service.py -v`

- [ ] **Step 5: Commit**

```bash
git add src/oa_knowledge/rebuild/markdown.py src/oa_knowledge/markdown_export/render.py tests/test_rebuild_markdown.py
git commit -m "feat: publish rebuilt body and attachment markdown"
```

### Task 4: Publish exactly one item index

**Files:**
- Create: `src/oa_knowledge/rebuild/index.py`
- Test: `tests/test_rebuild_index.py`

**Interfaces:**
- Produces: `publish_rebuilt_index(session: Session, settings: Settings, run_id: int, item_id: int) -> RebuildOutput`

- [ ] **Step 1: Write index completeness tests**

```python
def test_numbered_index_links_body_originals_and_markdown(session, settings, rebuilt_item):
    output = publish_rebuilt_index(session, settings, 1, rebuilt_item.id)
    text = resolve_target(settings, output.target_relpath).read_text()
    assert "正文" in text
    assert "原始附件" in text
    assert "附件 Markdown" in text

def test_index_is_unique_per_run_and_item(session, settings, rebuilt_item):
    first = publish_rebuilt_index(session, settings, 1, rebuilt_item.id)
    second = publish_rebuilt_index(session, settings, 1, rebuilt_item.id)
    assert second.id == first.id
```

- [ ] **Step 2: Run tests and confirm failure**

Run: `uv run pytest tests/test_rebuild_index.py -v`

- [ ] **Step 3: Implement the index**

List every preserved original. Link body only for numbered items. List successful attachment Markdown, unsupported formats, and retryable failures. Reject an unconfirmed classification, missing effective date, missing numbered body, broken target, or duplicate index.

- [ ] **Step 4: Run focused tests**

Run: `uv run pytest tests/test_rebuild_index.py tests/test_rebuild_markdown.py -v`

- [ ] **Step 5: Commit**

```bash
git add src/oa_knowledge/rebuild/index.py tests/test_rebuild_index.py
git commit -m "feat: publish rebuilt item indexes"
```

### Task 5: Orchestrate Markdown rebuild and expose progress

**Files:**
- Modify: `src/oa_knowledge/rebuild/campaign.py`
- Modify: `src/oa_knowledge/web/worker.py`
- Modify: `src/oa_knowledge/web/rebuild_views.py`
- Modify: `src/oa_knowledge/web/app.py`
- Modify: `webui/src/views/RebuildClassificationView.tsx`
- Test: `tests/test_rebuild_campaign.py`
- Test: `tests/test_web_rebuild_classification.py`
- Modify: `tests/test_worker.py`

**Interfaces:**
- Produces stages: `rebuild_parse`, `rebuild_publish`, `rebuild_index` in existing `PipelineTask`.
- Produces: `enqueue_markdown_rebuild(session: Session, run_id: int, item_ids: Sequence[int]) -> int`.
- Produces: `POST /api/rebuild/start`, `POST /api/rebuild/pause`, `POST /api/rebuild/resume`, `GET /api/rebuild/status`.

- [ ] **Step 1: Write end-to-end synthetic stage tests**

```python
def test_confirmed_item_reaches_rebuilt_index(rebuild_worker, confirmed_item):
    rebuild_worker.run_until_idle()
    assert successful_output(confirmed_item.id, "item_index")

def test_unconfirmed_item_is_never_enqueued(session, rebuild_run, needs_review_item):
    created = enqueue_markdown_rebuild(session, rebuild_run.id, [needs_review_item.id])
    assert created == 0
```

- [ ] **Step 2: Run tests and confirm failure**

Run: `uv run pytest tests/test_rebuild_campaign.py tests/test_worker.py -k rebuild -v`

- [ ] **Step 3: Implement resumable stages and progress APIs**

Use idempotency keys `rebuild:<run_id>:<stage>:<item-or-file-id>:<source-sha>`. Pause only prevents new claims; a running atomic file operation finishes. API responses contain counts, dates, and redacted error codes only.

- [ ] **Step 4: Run phase gate**

Run: `uv run pytest tests/test_rebuild_campaign.py tests/test_rebuild_body_source.py tests/test_rebuild_parser.py tests/test_rebuild_markdown.py tests/test_rebuild_index.py tests/test_worker.py -k 'rebuild or retired' -v`

Run: `cd webui && npm run check && npm run build`

Run: `uv run python scripts/check_public_release.py && uv run pytest`

- [ ] **Step 5: Commit**

```bash
git add src/oa_knowledge/rebuild src/oa_knowledge/web webui/src src/oa_knowledge/web/static tests
git commit -m "feat: run resumable markdown knowledge rebuild"
```
