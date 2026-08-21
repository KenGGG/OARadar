# OARadar Data Rebuild Phase 1 Classification Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a WebUI review gate that separates Done items into internal, external, and needs-review groups and persists explicit human confirmation before any rebuilt Markdown path can be created.

**Architecture:** Extend `OAItem` with minimal suggestion/confirmation fields, keep rule-based classification in a focused rebuild module, expose only narrow V2 rebuild APIs, and add one temporary daily-console page. Existing `PipelineTask` remains the task mechanism; this phase creates no rebuild files.

**Tech Stack:** Python 3.12, SQLAlchemy 2, Alembic, FastAPI, pytest, React, TypeScript, Vite.

**Spec:** `docs/superpowers/specs/2026-08-21-oaradar-data-cleanup-markdown-rebuild-design.md`

## Global Constraints

- OA access remains read-only; this phase must not start a browser or call OA.
- Store OA identifiers as text and never expose real OA body content in logs, tests, or Git.
- Use only synthetic fixtures.
- No confirmed classification means no final Markdown directory.
- Internal categories are exactly: 公司治理、经营管理、业务项目、风险管理、财务资金、人力行政、信息化、其他内部.
- External grouping uses the confirmed normalized institution name, not province/city/district buckets.
- Clear internal and clear external items may be confirmed in bulk; needs-review items require individual confirmation.

---

### Task 1: Persist classification suggestions and confirmations

**Files:**
- Create: `src/oa_knowledge/db/migrations/versions/0036_rebuild_classification_gate.py`
- Modify: `src/oa_knowledge/db/models.py`
- Test: `tests/test_database.py`

**Interfaces:**
- Produces: `OAItem.document_date: date | None`
- Produces: `OAItem.classification_state: str` with `suggested | needs_review | confirmed`
- Produces: `OAItem.classification_confidence: float | None`
- Produces: `OAItem.classification_confirmed_at: datetime | None`
- Produces: `OAItem.classification_source: str | None` with `rule | manual`

- [ ] **Step 1: Write migration tests that start at revision 0035**

```python
def test_0036_adds_rebuild_classification_gate(existing_0035_database):
    upgrade(existing_0035_database)
    columns = table_columns(existing_0035_database, "oa_items")
    assert {"document_date", "classification_state", "classification_confidence",
            "classification_confirmed_at", "classification_source"} <= columns
    row = fetch_item(existing_0035_database)
    assert row["classification_state"] == "needs_review"
```

- [ ] **Step 2: Run the migration test and confirm it fails**

Run: `uv run pytest tests/test_database.py -k 0036 -v`

Expected: FAIL because revision 0036 and model fields do not exist.

- [ ] **Step 3: Add the model fields and a SQLite-safe Alembic migration**

Use `op.batch_alter_table("oa_items", recreate="always")`. Add a check constraint for the three classification states and an index on `(source_channel, classification_state, source_type)`.

- [ ] **Step 4: Run migration and model tests**

Run: `uv run pytest tests/test_database.py -k '0036 or oa_item' -v`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/oa_knowledge/db/models.py src/oa_knowledge/db/migrations/versions/0036_rebuild_classification_gate.py tests/test_database.py
git commit -m "feat: persist rebuild classification confirmations"
```

### Task 2: Generate safe classification suggestions

**Files:**
- Create: `src/oa_knowledge/rebuild/__init__.py`
- Create: `src/oa_knowledge/rebuild/classification.py`
- Modify: `src/oa_knowledge/config.py`
- Modify: `config.example.yaml`
- Test: `tests/test_rebuild_classification.py`
- Modify: `tests/test_config.py`

**Interfaces:**
- Produces: `ClassificationDecision(source_type: str, internal_category: str | None, external_issuer: str | None, confidence: float, state: str)`
- Produces: `RebuildConfig(external_issuer_aliases: dict[str, str] = {})` as `Settings.rebuild`.
- Produces: `suggest_classification(item: OAItem, issuer_aliases: dict[str, str]) -> ClassificationDecision`
- Produces: `confirm_classification(session: Session, item_id: int, *, source_type: str, internal_category: str | None, external_issuer: str | None, confirmed_at: datetime) -> OAItem`
- Produces: `bulk_confirm_suggested(session: Session, source_type: Literal["internal", "external"], confirmed_at: datetime) -> int`

- [ ] **Step 1: Write rule and validation tests**

```python
def test_internal_suggestion_uses_fixed_category(done_item):
    done_item.title = "内部风险检查事项"
    result = suggest_classification(done_item, {})
    assert result.source_type == "internal"
    assert result.internal_category == "风险管理"

def test_external_alias_is_normalized(done_item):
    done_item.sender = "示例市工信局"
    result = suggest_classification(done_item, {"示例市工信局": "示例市工业和信息化局"})
    assert result.external_issuer == "示例市工业和信息化局"

def test_unclear_item_requires_review(done_item):
    done_item.title = "普通事项"
    done_item.sender = None
    assert suggest_classification(done_item, {}).state == "needs_review"

def test_external_alias_config_requires_nonempty_names():
    with pytest.raises(ValidationError):
        RebuildConfig(external_issuer_aliases={"示例简称": ""})
```

- [ ] **Step 2: Run tests and confirm they fail**

Run: `uv run pytest tests/test_rebuild_classification.py -v`

Expected: FAIL because the rebuild classification module does not exist.

- [ ] **Step 3: Implement deterministic suggestions and strict confirmation validation**

Reject confirmation when internal category is outside the eight-value list, when external issuer is blank, or when `source_type` is not `internal|external`. Suggestions below confidence `0.90` use `needs_review`. Bulk confirmation selects only `state='suggested'`, confidence at least `0.90`, the requested source type, and structurally valid rows.

- [ ] **Step 4: Run focused tests**

Run: `uv run pytest tests/test_rebuild_classification.py -v`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/oa_knowledge/rebuild src/oa_knowledge/config.py config.example.yaml tests/test_rebuild_classification.py tests/test_config.py
git commit -m "feat: classify rebuild items for human review"
```

### Task 3: Add narrow classification review APIs

**Files:**
- Create: `src/oa_knowledge/web/rebuild_views.py`
- Modify: `src/oa_knowledge/web/app.py`
- Test: `tests/test_web_rebuild_classification.py`
- Modify: `tests/test_web_v2_contract.py`

**Interfaces:**
- Produces: `GET /api/rebuild/classifications?group=internal|external|needs_review&page=1&page_size=50`
- Produces: `POST /api/rebuild/classifications/{item_id}/confirm`
- Produces: `POST /api/rebuild/classifications/bulk-confirm` with body `{"source_type": "internal"}` or `{"source_type": "external"}`
- Produces: `GET /api/rebuild/classification-summary`

- [ ] **Step 1: Write API contract tests**

```python
def test_needs_review_page_redacts_body(client, seeded_items):
    payload = client.get("/api/rebuild/classifications?group=needs_review").json()
    assert set(payload["items"][0]) == {
        "id", "title", "document_number", "sender", "item_date",
        "source_type", "internal_category", "external_issuer",
        "classification_state", "has_document_number", "attachment_count",
    }
    assert "body" not in payload["items"][0]

def test_bulk_confirm_does_not_confirm_needs_review(client, seeded_items):
    response = client.post("/api/rebuild/classifications/bulk-confirm", json={"source_type": "internal"})
    assert response.status_code == 200
    assert response.json()["needs_review_unchanged"] > 0
```

- [ ] **Step 2: Run tests and confirm 404/failure**

Run: `uv run pytest tests/test_web_rebuild_classification.py -v`

- [ ] **Step 3: Implement pagination, confirmation, and JSON errors**

Use existing loopback/origin/CSRF middleware. Return 409 for invalid confirmation transitions, 422 for invalid category/institution input, and JSON 404 for unknown item IDs.

- [ ] **Step 4: Run API and security tests**

Run: `uv run pytest tests/test_web_rebuild_classification.py tests/test_web_security.py tests/test_web_v2_contract.py -v`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/oa_knowledge/web/rebuild_views.py src/oa_knowledge/web/app.py tests/test_web_rebuild_classification.py tests/test_web_v2_contract.py
git commit -m "feat: expose rebuild classification review api"
```

### Task 4: Build the WebUI classification page

**Files:**
- Create: `webui/src/views/RebuildClassificationView.tsx`
- Create: `webui/src/types/rebuild.ts`
- Modify: `webui/src/App.tsx`
- Modify: `webui/src/styles.css`
- Test: `tests/test_frontend_v2_assets.py`

**Interfaces:**
- Consumes: Phase 1 Task 3 APIs.
- Produces: `View = "overview" | "pending" | "done" | "markdown" | "settings" | "rebuild"` and a “资料重建” navigation entry.
- Produces: group tabs, paginated title list, single confirmation form, and bulk confirm controls for suggested internal/external rows only.

- [ ] **Step 1: Add static asset contract tests**

```python
def test_rebuild_page_exposes_three_review_groups():
    source = Path("webui/src/views/RebuildClassificationView.tsx").read_text()
    assert all(label in source for label in ("内部事项", "外部事项", "待确认事项"))
    assert "确认全部明确的内部事项" in source
    assert "确认全部明确的外部事项" in source
```

- [ ] **Step 2: Run the test and confirm it fails**

Run: `uv run pytest tests/test_frontend_v2_assets.py -v`

- [ ] **Step 3: Implement the typed page**

Do not render OA body text. Require a confirmation dialog before bulk confirmation. Disable final confirmation until internal category or external institution is valid.

- [ ] **Step 4: Run frontend and backend checks**

Run: `cd webui && npm run check && npm run build`

Run: `uv run pytest tests/test_frontend_v2_assets.py tests/test_web_rebuild_classification.py -v`

Expected: PASS.

- [ ] **Step 5: Run the phase gate and commit**

Run: `uv run python scripts/check_public_release.py && uv run pytest`

```bash
git add webui/src webui/package-lock.json src/oa_knowledge/web/static tests/test_frontend_v2_assets.py
git commit -m "feat: add rebuild classification review page"
```
