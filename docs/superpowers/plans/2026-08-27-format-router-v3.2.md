# Format Router v3.2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reliably identify each OA attachment's actual local format and use one shared, quality-gated Markdown route for candidate and formal builds.

**Architecture:** A new `FormatRouter` identifies content from a normalized OA display name plus file signatures and Office container streams. It returns an immutable route decision (actual type, source of detection, primary parser, single fallback, or precise unsupported/container reason). Both candidate backfill and formal export consume that decision; archive contents remain package-driven rather than becoming an originals-directory batch job.

**Tech Stack:** Python 3.12, `markitdown[xls]`/`xlrd`, local `wv`, local MinerU, optional local LibreOffice, pytest.

**Spec:** User-approved v3.2 boundaries in this conversation, 2026-08-27.

## Global Constraints

- Never rename, move, delete, or overwrite `data/originals/`.
- Detect actual format from signatures/container streams before fallback to a normalized display suffix.
- OLE must distinguish DOC (`WordDocument`), XLS (`Workbook`/`Book`), PPT (`PowerPoint Document`), and `unknown_ole`.
- Each parse permits one primary engine and at most one fallback.
- Images (`png`, `jpg`, `jpeg`, `tif`, `tiff`, `bmp`) use MinerU first.
- ZIP remains a separately controlled archive container; RAR/7z report `archive_container_unsupported`.
- Candidate packages only publish classified items; formal publication never emits needs-review packages.

---

### Task 1: Actual-type detector and route decisions

**Files:**
- Create: `src/oa_knowledge/parsers/format_router.py`
- Test: `tests/test_format_router.py`

**Interfaces:**
- Produces `FormatDecision(actual_file_type, detection_source, primary_engine, fallback_engine, status_code)`.
- Produces `detect_format(path) -> FormatDecision` and `parser_attempts(decision, mineru_enabled) -> tuple[str, ...]`.

- [ ] **Step 1: Write failing detector tests**

```python
def test_detector_strips_display_size_suffix_and_uses_pdf_signature(tmp_path):
    source = tmp_path / "notice.pdf (1M)"
    source.write_bytes(b"%PDF-1.7\n")
    decision = detect_format(source)
    assert decision.actual_file_type == "pdf"
    assert decision.detection_source == "file_signature"

def test_detector_distinguishes_ole_streams(tmp_path, monkeypatch):
    source = tmp_path / "attachment.doc (6M)"
    source.write_bytes(COMPOUND_HEADER)
    monkeypatch.setattr(format_router, "_ole_streams", lambda _: {"Workbook"})
    assert detect_format(source).actual_file_type == "xls"
```

- [ ] **Step 2: Run detector tests and verify failure**

Run: `uv run pytest tests/test_format_router.py -q`

- [ ] **Step 3: Implement minimal detector and route map**

Implement suffix normalization (` (nM)`, `_ target=`), signatures for PDF, OLE, ZIP-based Office and common images, OLE stream inspection, and precise unsupported/container decisions.

- [ ] **Step 4: Re-run detector tests**

Run: `uv run pytest tests/test_format_router.py -q`

### Task 2: Shared parser dispatcher, XLS and legacy DOC routes

**Files:**
- Modify: `src/oa_knowledge/parsers/router.py`
- Modify: `pyproject.toml`
- Modify: `uv.lock`
- Test: `tests/test_format_router.py`
- Test: `tests/test_parsers.py`

**Interfaces:**
- Consumes `FormatDecision`; `parse_file` dispatches based on actual type, never `Path.suffix`.
- Adds `libreoffice` as an XLS fallback engine only.

- [ ] **Step 1: Write failing parser-route tests**

```python
def test_legacy_doc_routes_to_wv_after_markitdown_failure(...):
    assert parser_attempts(detect_format(source), mineru_enabled=True) == ("markitdown", "wv")

def test_xls_routes_to_markitdown_then_libreoffice(...):
    assert parser_attempts(detect_format(source), mineru_enabled=True) == ("markitdown", "libreoffice")
```

- [ ] **Step 2: Run route tests and verify failure**

Run: `uv run pytest tests/test_format_router.py tests/test_parsers.py -q`

- [ ] **Step 3: Add `markitdown[xls]`, dispatcher support and LibreOffice fallback**

Use MarkItDown `XlsConverter` as the primary reader. LibreOffice creates only a temporary `.xlsx`, invokes MarkItDown once, then removes the temporary directory.

- [ ] **Step 4: Re-run route tests**

Run: `uv run pytest tests/test_format_router.py tests/test_parsers.py -q`

### Task 3: Candidate backfill integration and quality/date corrections

**Files:**
- Modify: `src/oa_knowledge/backfill_mvp.py`
- Modify: `src/oa_knowledge/classification/internal_classification.py`
- Test: `tests/test_backfill_mvp.py`
- Test: `tests/test_internal_classification.py`

**Interfaces:**
- Candidate metadata records `actual_file_type`, detection source, route attempts, and fallback reason.
- Package dates use `completed_at`, then `initiated_at`, then `received_at`.

- [ ] **Step 1: Write failing integration tests**

Cover decorated XLS conversion, `doc (6M)` detection/routing, BMP MinerU primary, archive-container reason, structured short-line acceptance, and initiated-date fallback.

- [ ] **Step 2: Run tests and verify failure**

Run: `uv run pytest tests/test_backfill_mvp.py tests/test_internal_classification.py -q`

- [ ] **Step 3: Integrate shared decisions and minimal classification fixes**

Replace suffix lists with the router; preserve quality gates except for recognized tables/key-value forms; add approved report, meeting, banking, and inquiry subject rules.

- [ ] **Step 4: Re-run integration tests**

Run: `uv run pytest tests/test_backfill_mvp.py tests/test_internal_classification.py -q`

### Task 4: Formal Markdown service integration

**Files:**
- Modify: `src/oa_knowledge/markdown_export/service.py`
- Test: `tests/test_markdown_export_service.py`

**Interfaces:**
- Formal export queries `FormatRouter`; it shares parsers/routes with the candidate service.
- RAR/7z use `ARCHIVE_CONTAINER_UNSUPPORTED`; OFD/WPS/ET/CEB use explicit unsupported type codes; MP4 emits metadata only.

- [ ] **Step 1: Write failing formal-export tests**

Cover a decorated DOC route, legacy XLS parser hint, and precise archive/unsupported codes without publication of non-convertible body text.

- [ ] **Step 2: Run formal-export tests and verify failure**

Run: `uv run pytest tests/test_markdown_export_service.py -q`

- [ ] **Step 3: Replace formal suffix lists with shared router calls**

Do not add an originals-directory full conversion invocation; retain existing OA package/export drivers.

- [ ] **Step 4: Re-run formal-export tests**

Run: `uv run pytest tests/test_markdown_export_service.py -q`

### Task 5: Compatibility inventory and 73-item v3.2 regression

**Files:**
- Create: runtime-local compatibility report under `~/.local/state/oaradar/reports/`
- Test: `tests/test_format_router.py`

- [ ] **Step 1: Add a pure inventory aggregation test**

Verify counts aggregate by `actual_file_type`, normalized-name corrections, containers, unsupported reasons, excluded status, and unique SHA256.

- [ ] **Step 2: Run the inventory test and verify failure**

Run: `uv run pytest tests/test_format_router.py -q`

- [ ] **Step 3: Produce local full-format report and fresh v3.2 candidate**

Inventory all 15,387 originals read-only, then rerun only the fixed 73 OA keys into a new verified candidate directory.

- [ ] **Step 4: Verify reconciliation and idempotency**

Run focused tests, full suite, lint, candidate hash reconciliation, originals mtime/ctime snapshot comparison, and one idempotent rerun.
