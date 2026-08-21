# OARadar Data Rebuild Phase 2 Clean Archive Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a verified `data_rebuilt/archive/` from the current local evidence without changing OA or the live `data/` tree.

**Architecture:** Produce a confidential local inventory, derive every destination deterministically, and copy each verified source through a temporary file followed by SHA256 verification and atomic rename. Reuse `PipelineRun`, `PipelineTask`, and `PipelineEvent` for resumable work; add only a rebuild-output ledger, not another queue framework.

**Tech Stack:** Python 3.12, SQLAlchemy 2, Alembic, pathlib, hashlib, shutil, Typer, pytest.

**Spec:** `docs/superpowers/specs/2026-08-21-oaradar-data-cleanup-markdown-rebuild-design.md`

## Global Constraints

- This phase is local-only and must never access OA.
- Never overwrite, move, chmod, or delete any file below the live `data/`.
- Only `download_status='verified'` files with matching size and SHA256 may enter the rebuilt archive.
- Container traversal depth remains at most 10; depth-limit items remain incomplete.
- Inventory output is confidential, ignored by Git, and never prints titles or paths to stdout.
- The target root must resolve outside the live `data_root` and must not be `/`, a home directory, or the repository root.

---

### Task 1: Add rebuild configuration and protected target-path validation

**Files:**
- Modify: `src/oa_knowledge/config.py`
- Modify: `config.example.yaml`
- Create: `src/oa_knowledge/rebuild/paths.py`
- Test: `tests/test_rebuild_paths.py`
- Modify: `tests/test_config.py`

**Interfaces:**
- Extends: Phase 1 `RebuildConfig` with `target_root: Path = Path("../data_rebuilt")` and `item_title_max_chars: int = 96`.
- Produces: `resolve_rebuild_root(settings: Settings) -> Path`
- Produces: `safe_component(value: str, *, max_chars: int = 96) -> str`
- Produces: `effective_item_date(item: OAItem) -> date`
- Produces: `archive_item_relpath(item: OAItem) -> PurePosixPath`
- Produces: `archive_file_relpath(item: OAItem, source: ArchivedFile) -> PurePosixPath`
- Produces: `markdown_item_relpath(item: OAItem) -> PurePosixPath`
- Produces: `resolve_rebuild_path(settings: Settings, relpath: str | PurePosixPath) -> Path`

- [ ] **Step 1: Write path tests**

```python
def test_internal_markdown_path_uses_category_year_month(item):
    item.source_type = "internal"
    item.internal_category = "风险管理"
    item.document_date = date(2026, 8, 20)
    item.classification_state = "confirmed"
    path = markdown_item_relpath(item)
    assert path.parts[:4] == ("markdown", "内部事项", "风险管理", "2026年")
    assert path.parts[4] == "08月"

def test_external_markdown_path_uses_exact_institution(item):
    item.source_type = "external"
    item.external_issuer = "示例市工业和信息化局"
    item.document_date = date(2026, 8, 20)
    item.classification_state = "confirmed"
    assert markdown_item_relpath(item).parts[2] == "示例市工业和信息化局"

def test_archive_path_is_stable_when_classification_changes(item, archived_file):
    before = archive_file_relpath(item, archived_file)
    item.internal_category = "财务资金"
    after = archive_file_relpath(item, archived_file)
    assert before == after
    assert before.parts[:3] == ("archive", "oa", "done")
```

- [ ] **Step 2: Run tests and confirm failure**

Run: `uv run pytest tests/test_rebuild_paths.py tests/test_config.py -v`

- [ ] **Step 3: Implement protected paths**

Use `archive/oa/done/<YYYY>/<MM>/<stable-item-folder>/<original-name>` for original evidence; archive paths never contain the classification. Use the confirmed internal/external tree from the spec only for Markdown. Reject unconfirmed classification, absent effective date, absolute paths, `..`, control characters, empty names, and targets equal to or nested above the live root. Append `--<8-char oa_item_key sha256>` after deterministic truncation to prevent collisions.

- [ ] **Step 4: Run focused tests**

Run: `uv run pytest tests/test_rebuild_paths.py tests/test_config.py -v`

- [ ] **Step 5: Commit**

```bash
git add src/oa_knowledge/config.py config.example.yaml src/oa_knowledge/rebuild/paths.py tests/test_rebuild_paths.py tests/test_config.py
git commit -m "feat: define protected rebuild paths"
```

### Task 2: Build a confidential read-only inventory

**Files:**
- Create: `src/oa_knowledge/rebuild/inventory.py`
- Create: `tests/test_rebuild_inventory.py`

**Interfaces:**
- Produces: `InventoryRow(item_id: int, file_id: int, source_relpath: str, destination_relpath: str, size_bytes: int, sha256: str, file_role: str, status: str)`
- Produces: `build_inventory(session: Session, settings: Settings) -> list[InventoryRow]`
- Produces: `inventory_summary(rows: Sequence[InventoryRow]) -> dict[str, int]`
- Produces: `write_private_inventory(target: Path, rows: Sequence[InventoryRow]) -> None`

- [ ] **Step 1: Write tests for verified, missing, mismatched, and depth-limit files**

```python
def test_inventory_admits_only_verified_hash_matches(session, settings, archived_file):
    rows = build_inventory(session, settings)
    row = next(row for row in rows if row.file_id == archived_file.id)
    assert row.status == "ready"

def test_inventory_does_not_print_confidential_paths(capsys, rows):
    inventory_summary(rows)
    assert capsys.readouterr().out == ""
```

- [ ] **Step 2: Run tests and confirm failure**

Run: `uv run pytest tests/test_rebuild_inventory.py -v`

- [ ] **Step 3: Implement the inventory**

Use `resolve_data_path` for live sources. Store full rows only below `data_rebuilt/state/private/` with mode `0600`; return only counts to CLI and Web APIs. Mark `missing`, `hash_mismatch`, `unsafe_path`, and `depth_limit_reached` explicitly.

- [ ] **Step 4: Run focused tests**

Run: `uv run pytest tests/test_rebuild_inventory.py tests/test_storage_paths.py -v`

- [ ] **Step 5: Commit**

```bash
git add src/oa_knowledge/rebuild/inventory.py tests/test_rebuild_inventory.py
git commit -m "feat: inventory rebuild source evidence"
```

### Task 3: Copy originals atomically and record resumable outputs

**Files:**
- Create: `src/oa_knowledge/db/migrations/versions/0037_rebuild_outputs.py`
- Modify: `src/oa_knowledge/db/models.py`
- Create: `src/oa_knowledge/rebuild/archive_copy.py`
- Test: `tests/test_rebuild_archive_copy.py`
- Modify: `tests/test_database.py`

**Interfaces:**
- Produces: `RebuildOutput(run_id: int, oa_item_id: int, source_file_id: int | None, kind: str, target_relpath: str, sha256: str | None, status: str, error_code: str | None)`
- Produces: `copy_inventory_row(session: Session, settings: Settings, row: InventoryRow) -> RebuildOutput`
- Produces: output kinds `original | parse | body_markdown | attachment_markdown | item_index`.

- [ ] **Step 1: Write migration and atomic-copy tests**

```python
def test_copy_verifies_hash_before_success(session, settings, inventory_row):
    output = copy_inventory_row(session, settings, inventory_row)
    assert output.status == "success"
    assert sha256_file(resolve_target(settings, output.target_relpath)) == inventory_row.sha256

def test_copy_is_idempotent(session, settings, inventory_row):
    first = copy_inventory_row(session, settings, inventory_row)
    second = copy_inventory_row(session, settings, inventory_row)
    assert second.id == first.id
```

- [ ] **Step 2: Run tests and confirm failure**

Run: `uv run pytest tests/test_database.py -k 0037 tests/test_rebuild_archive_copy.py -v`

- [ ] **Step 3: Implement ledger and atomic copy**

Copy to a temporary file inside the target directory, fsync, verify size and SHA256, then `os.replace`. If a current target already matches, return success without copying. Never replace a different target; record `TARGET_CONFLICT` and stop that item.

- [ ] **Step 4: Run focused tests**

Run: `uv run pytest tests/test_database.py -k 0037 tests/test_rebuild_archive_copy.py -v`

- [ ] **Step 5: Commit**

```bash
git add src/oa_knowledge/db/models.py src/oa_knowledge/db/migrations/versions/0037_rebuild_outputs.py src/oa_knowledge/rebuild/archive_copy.py tests/test_database.py tests/test_rebuild_archive_copy.py
git commit -m "feat: copy verified originals into clean archive"
```

### Task 4: Add dry-run and resumable archive-build commands

**Files:**
- Create: `src/oa_knowledge/rebuild/campaign.py`
- Modify: `src/oa_knowledge/cli.py`
- Test: `tests/test_rebuild_cli.py`
- Test: `tests/test_rebuild_campaign.py`

**Interfaces:**
- Produces: `create_rebuild_run(session: Session, *, cutoff_at: datetime) -> PipelineRun`
- Produces: `enqueue_archive_copy(session: Session, run_id: int, rows: Sequence[InventoryRow]) -> int`
- Produces CLI: `oa rebuild inventory --config config.yaml`
- Produces CLI: `oa rebuild archive --config config.yaml --execute`
- Produces CLI: `oa rebuild status --config config.yaml`

- [ ] **Step 1: Write CLI safety and resume tests**

```python
def test_archive_command_defaults_to_dry_run(runner, config_file):
    result = runner.invoke(app, ["rebuild", "archive", "--config", str(config_file)])
    assert result.exit_code == 0
    assert json.loads(result.stdout)["copied"] == 0

def test_resume_does_not_recopy_successful_output(queue, completed_output):
    rows = [inventory_row_for(completed_output.source_file_id)]
    assert enqueue_archive_copy(queue.session, completed_output.run_id, rows) == 0
```

- [ ] **Step 2: Run tests and confirm failure**

Run: `uv run pytest tests/test_rebuild_cli.py tests/test_rebuild_campaign.py -v`

- [ ] **Step 3: Implement commands using existing pipeline tables**

Use queue name `data_rebuild`, stages `inventory` and `archive_copy`, idempotency key `rebuild:<run_id>:archive:<file_id>:<sha256>`. CLI output contains counts and error codes only, never titles or paths.

- [ ] **Step 4: Run phase gate**

Run: `uv run pytest tests/test_rebuild_cli.py tests/test_rebuild_campaign.py tests/test_rebuild_inventory.py tests/test_rebuild_archive_copy.py -v`

Run: `uv run python scripts/check_public_release.py && uv run pytest`

- [ ] **Step 5: Commit**

```bash
git add src/oa_knowledge/rebuild/campaign.py src/oa_knowledge/cli.py tests/test_rebuild_cli.py tests/test_rebuild_campaign.py
git commit -m "feat: orchestrate clean archive rebuild"
```
