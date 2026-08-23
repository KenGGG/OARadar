# OARadar Minimal Data and Local Classification Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `data/` contain only immutable OA originals and classified Markdown while preserving the three production flows with local, bounded Qwen classification.

**Architecture:** Add explicit XDG state/cache roots to `Settings`, replace legacy `data/` path assumptions with `originals/` and `markdown/`, and provide a guarded local migration command. Reuse the current SQLAlchemy ledger and pipeline, but select historical jobs from locally reverified originals rather than online-audit state. Classification is a rule-first, bounded map/reduce service that returns a valid category or `unclassified`; it never blocks Markdown publication.

**Tech Stack:** Python 3.12, SQLAlchemy 2, Alembic, Typer, FastAPI, React, Ollama/Qwen, pytest, Vite.

**Spec:** `docs/superpowers/specs/2026-08-23-oaradar-minimal-data-classification-design.md`

## Global Constraints

- OA and Feishu integration remain read-only during migration and verification.
- OA identifiers are text; archived paths are POSIX paths relative to `data_root`.
- `data/` ends with exactly `originals/` and `markdown/`; database, browser profile, cache, backups, reports, logs, locks, quarantine, parser products, and legacy projections do not remain below it.
- Only `direct_attachment`, `official_attachment`, `official_body`, and original containers with matching size and SHA256 are retained as originals.
- Never overwrite or delete an original; container traversal never reports success beyond depth 10.
- Model prompts/responses are local-only and never logged or committed. Model requests use a 2,000-token map budget, 8,000-token absolute reduce budget, 512-token output cap, 1,024-token margin, and concurrency one.
- A low-confidence, invalid, or unavailable model result publishes to `unclassified/`; it never blocks Markdown.
- Live migration is dry-run by default, uses a SQLite backup, verifies every original before hard-link/copy, performs same-filesystem rename-only cutover, and rolls back renames on smoke failure.

---

### Task 1: Add XDG runtime roots and the final data-root path contract

**Files:**
- Modify: `src/oa_knowledge/config.py`
- Modify: `config.example.yaml`
- Create: `src/oa_knowledge/runtime_paths.py`
- Modify: `src/oa_knowledge/storage_paths.py`
- Test: `tests/test_runtime_paths.py`
- Modify: `tests/test_config.py`

**Interfaces:**
- Produces `RuntimeConfig(state_root: Path, cache_root: Path)` with defaults `~/.local/state/oaradar` and `~/.cache/oaradar`.
- Produces `Settings.state_root`, `Settings.cache_root`, `Settings.database_path`, `Settings.browser_profile_path`, `Settings.parse_work_root`, `Settings.originals_root`, and `Settings.markdown_root`.
- Produces `ensure_owned_directory(path: Path, *, mode: int = 0o700) -> Path` and `resolve_original_path(settings, relpath) -> Path`.

- [ ] **Step 1: Write failing configuration and path-boundary tests**

```python
def test_settings_places_database_and_browser_outside_data_root(tmp_path):
    settings = Settings.model_validate({"app": {"data_root": tmp_path / "data"}})
    assert settings.database_path == settings.state_root / "oa.db"
    assert settings.browser_profile_path == settings.cache_root / "browser-profile"
    assert settings.originals_root == settings.data_root / "originals"
    assert settings.markdown_root == settings.data_root / "markdown"

def test_resolve_original_path_rejects_legacy_and_escape_paths(settings):
    with pytest.raises(ValueError):
        resolve_original_path(settings, "raw/done/x.pdf")
    with pytest.raises(ValueError):
        resolve_original_path(settings, "originals/../state/oa.db")
```

- [ ] **Step 2: Run the focused tests and confirm they fail because the new contract is absent**

Run: `uv run pytest tests/test_runtime_paths.py tests/test_config.py -v`

- [ ] **Step 3: Implement the contract**

Keep `app.data_root` relative/absolute handling intact. Add a separate `runtime` config section that permits only absolute roots after expansion and rejects roots equal to, inside, or above `data_root`. Make `storage.sqlite_path` a filename relative to `state_root`; make the browser profile and parse work paths derive from `cache_root`. Set original and Markdown roots directly under `data_root`. Do not create directories during settings parsing.

- [ ] **Step 4: Run focused tests and the existing storage-path tests**

Run: `uv run pytest tests/test_runtime_paths.py tests/test_config.py tests/test_storage_paths.py -v`

- [ ] **Step 5: Commit**

```bash
git add src/oa_knowledge/config.py src/oa_knowledge/runtime_paths.py src/oa_knowledge/storage_paths.py config.example.yaml tests/test_runtime_paths.py tests/test_config.py
git commit -m "feat: separate OARadar runtime from data"
```

### Task 2: Rebase original-file persistence and temporary parsing on the final paths

**Files:**
- Modify: `src/oa_knowledge/archive/writer.py`
- Modify: `src/oa_knowledge/detail_archive.py`
- Modify: `src/oa_knowledge/pipeline.py`
- Modify: `src/oa_knowledge/source_markdown/service.py`
- Modify: `src/oa_knowledge/markdown_export/paths.py`
- Test: `tests/test_original_storage.py`
- Modify: `tests/test_archive.py`, `tests/test_source_markdown_service.py`, `tests/test_markdown_export_paths.py`

**Interfaces:**
- Produces only `originals/...` `ArchivedFile.local_relpath` values for newly downloaded Done originals.
- Produces `temporary_parse_directory(settings, source_sha256) -> Path` under `cache_root/work`.
- Produces `publish_attachment_markdown(session, settings, source_file_id, classification) -> MarkdownExport` and removes the temporary parser product after successful publication.

- [ ] **Step 1: Write failing tests for final paths and no retained parser product**

```python
def test_done_download_is_recorded_below_originals(settings, archived_done_fixture):
    file = archived_done_fixture.file
    assert file.local_relpath.startswith("originals/")
    assert (settings.data_root / file.local_relpath).is_file()

def test_successful_markdown_publish_removes_temporary_parse_product(settings, session, parsed_file):
    publish_attachment_markdown(session, settings, parsed_file.id, unclassified())
    assert not temporary_parse_directory(settings, parsed_file.sha256).exists()
```

- [ ] **Step 2: Run the focused tests and confirm they fail on legacy roots/persistent parse products**

Run: `uv run pytest tests/test_original_storage.py tests/test_source_markdown_service.py tests/test_markdown_export_paths.py -v`

- [ ] **Step 3: Implement the final storage behavior**

Use a stable classification-independent original path under `originals/done/<year>/<month>/<safe-item-key>/`. Preserve the `ArchivedFile` hash check before every parse. Parse to a cache work directory, publish Markdown through an atomic temporary destination, copy only referenced assets, then delete the temporary directory after commit-safe success. On failure retain only the active retry directory under cache; never write parser products under `data/`.

- [ ] **Step 4: Run focused archive and Markdown tests**

Run: `uv run pytest tests/test_original_storage.py tests/test_archive.py tests/test_source_markdown_service.py tests/test_markdown_export_paths.py -v`

- [ ] **Step 5: Commit**

```bash
git add src/oa_knowledge/archive src/oa_knowledge/detail_archive.py src/oa_knowledge/pipeline.py src/oa_knowledge/source_markdown src/oa_knowledge/markdown_export tests/test_original_storage.py tests/test_archive.py tests/test_source_markdown_service.py tests/test_markdown_export_paths.py
git commit -m "feat: retain only originals and final Markdown under data"
```

### Task 3: Implement bounded rule-first local Qwen classification

**Files:**
- Create: `src/oa_knowledge/classification/__init__.py`
- Create: `src/oa_knowledge/classification/models.py`
- Create: `src/oa_knowledge/classification/service.py`
- Modify: `src/oa_knowledge/enrich/context_budget.py`
- Modify: `src/oa_knowledge/enrich/llm_client.py`
- Modify: `src/oa_knowledge/config.py`
- Modify: `config.example.yaml`
- Test: `tests/test_local_classification.py`
- Modify: `tests/test_llm_context_budget.py`

**Interfaces:**
- Produces `ClassificationResult(source_type, internal_category, external_issuer, confidence, evidence_aliases, version, status)`.
- Produces `classify_done_markdown(item, sources, client, settings) -> ClassificationResult`.
- Produces `build_item_summary(sources, client, budget) -> str` with every `client.chat` input no greater than its `ContextBudget.max_input_tokens`.
- Adds `ClassificationConfig(map_input_tokens=2000, reduce_input_tokens=8000, max_output_tokens=512, confidence_threshold=0.75, max_concurrency=1)`.

- [ ] **Step 1: Write failing classification tests**

```python
def test_long_attachment_is_mapped_and_reduced_within_budget(fake_client, long_source, settings):
    result = classify_done_markdown(item(), [long_source], fake_client, settings)
    assert result.source_type == "internal"
    assert max(fake_client.input_token_estimates) <= 8_000

def test_invalid_model_alias_falls_back_to_unclassified(fake_client, source, settings):
    fake_client.reply_with_unknown_alias()
    result = classify_done_markdown(item(), [source], fake_client, settings)
    assert result.status == "unclassified"

def test_low_confidence_runs_one_verification_then_remains_nonblocking(fake_client, source, settings):
    fake_client.reply_low_confidence_twice()
    assert classify_done_markdown(item(), [source], fake_client, settings).source_type == "unclassified"
```

- [ ] **Step 2: Run the new classification tests and confirm they fail because the package is absent**

Run: `uv run pytest tests/test_local_classification.py tests/test_llm_context_budget.py -v`

- [ ] **Step 3: Implement schemas, rules, bounded map/reduce, and verification**

Use source aliases generated in memory (`S1`, `S2`, ...), fixed category literals, and JSON Schema validation. Map chunks at 2,000 tokens including prompt overhead, recursively reduce summaries without exceeding 8,000 tokens, then perform a final decision. Treat unavailable local model, malformed JSON, unsupported category, invented aliases, missing evidence, or low confidence after one verifier call as `unclassified`. Set the Ollama request `num_ctx` to the exact budget plus output and margin, never to the model maximum. Store no prompt or response body.

- [ ] **Step 4: Run classification and existing context/client tests**

Run: `uv run pytest tests/test_local_classification.py tests/test_llm_context_budget.py tests/test_enrich.py tests/test_curation_classifier.py -v`

- [ ] **Step 5: Commit**

```bash
git add src/oa_knowledge/classification src/oa_knowledge/enrich/context_budget.py src/oa_knowledge/enrich/llm_client.py src/oa_knowledge/config.py config.example.yaml tests/test_local_classification.py tests/test_llm_context_budget.py
git commit -m "feat: classify Markdown with bounded local Qwen"
```

### Task 4: Persist classifications and publish classified Markdown/indexes

**Files:**
- Create: `src/oa_knowledge/db/migrations/versions/0036_local_classification.py`
- Modify: `src/oa_knowledge/db/models.py`
- Modify: `src/oa_knowledge/markdown_delivery.py`
- Modify: `src/oa_knowledge/source_markdown/service.py`
- Test: `tests/test_classified_markdown.py`
- Modify: `tests/test_database.py`, `tests/test_markdown_delivery.py`

**Interfaces:**
- Adds `OAItem.classification_confidence`, `classification_status`, `classification_source_hash`, and `classification_version` fields.
- Produces `classify_and_publish_item(session, settings, oa_item_key) -> ClassificationResult`.
- Produces paths under `markdown/internal/...`, `markdown/external/...`, or `markdown/unclassified/...` with a unique item directory and one `_index.md`.

- [ ] **Step 1: Write failing persistence and publishing tests**

```python
def test_confirmed_internal_result_writes_only_classified_markdown_tree(session, settings, item):
    result = ClassificationResult.internal("风险管理", confidence=0.91, aliases=["S1"])
    publish_classified_item(session, settings, item, result)
    assert (settings.markdown_root / "internal" / "风险管理").exists()
    assert not (settings.data_root / "workspace").exists()

def test_unclassified_result_still_publishes_index_and_attachment_markdown(session, settings, item):
    publish_classified_item(session, settings, item, ClassificationResult.unclassified())
    assert item_index_path(settings, item).is_file()
```

- [ ] **Step 2: Run tests and confirm they fail because fields and classified destination logic are absent**

Run: `uv run pytest tests/test_classified_markdown.py tests/test_database.py -k '0036 or classified' -v`

- [ ] **Step 3: Implement migration and atomic classified publishing**

Persist only the normalized outcome, confidence, source-set hash, and version. Construct a deterministic safe item folder from date, document number/title, and OA-key hash. Write attachment Markdown and `_index.md` into a same-filesystem temporary item tree, verify every generated relative link stays below `data/`, then atomically promote it. On category change, promote the new tree before deleting the old path only when it is OARadar-managed.

- [ ] **Step 4: Run focused delivery and database tests**

Run: `uv run pytest tests/test_classified_markdown.py tests/test_markdown_delivery.py tests/test_source_markdown_service.py tests/test_database.py -v`

- [ ] **Step 5: Commit**

```bash
git add src/oa_knowledge/db src/oa_knowledge/markdown_delivery.py src/oa_knowledge/source_markdown/service.py tests/test_classified_markdown.py tests/test_markdown_delivery.py tests/test_database.py
git commit -m "feat: publish classified Done Markdown"
```

### Task 5: Simplify historical pipeline selection and retire non-core web routes

**Files:**
- Modify: `src/oa_knowledge/production_pipeline.py`
- Modify: `src/oa_knowledge/web/worker.py`
- Modify: `src/oa_knowledge/web/app.py`
- Modify: `src/oa_knowledge/web/console_views.py`
- Modify: `webui/src/App.tsx`
- Test: `tests/test_production_pipeline.py`
- Test: `tests/test_web_minimal_contract.py`
- Modify: `tests/test_web_v2_contract.py`, `tests/test_frontend_v2_assets.py`

**Interfaces:**
- Produces `local_original_is_verified(session, settings, item) -> bool` and historical task eligibility independent of `OnlineAuditRun`.
- Changes Done progression to `attachment_inventory -> parse -> classify -> publish -> index_publish`.
- Keeps only `/api/dashboard`, `/api/pending-notifications`, `/api/done-archives`, `/api/markdown-outputs`, `/api/settings`, authentication, health, and static SPA fallback routes.

- [ ] **Step 1: Write failing pipeline and API-contract tests**

```python
def test_verified_local_historical_item_claims_when_latest_online_audit_failed(queue, item):
    task = queue.claim("worker")
    assert task.logical_item_key == item.oa_item_key

def test_minimal_router_does_not_register_retired_governance_or_audit_routes(client):
    assert client.get("/api/audits/online").status_code == 404
    assert client.get("/api/data-governance").status_code == 404
```

- [ ] **Step 2: Run tests and confirm they fail because audit gating/routes remain**

Run: `uv run pytest tests/test_production_pipeline.py tests/test_web_minimal_contract.py -v`

- [ ] **Step 3: Implement local-evidence eligibility and minimal surface**

Before a historical task is claimed, resolve its `originals/` files and verify regular-file status, byte count, and SHA256. A bad source fails only that item with a stable error code. Remove online-audit eligibility predicates, retired task handlers, and retired API registrations. Do not remove database history or helpers required by the three flows. Make the React app contain only the five approved views.

- [ ] **Step 4: Run focused worker, API, and frontend tests**

Run: `uv run pytest tests/test_production_pipeline.py tests/test_web_minimal_contract.py tests/test_web_v2_contract.py tests/test_frontend_v2_assets.py -v`

Run: `cd webui && npm run check && npm run build`

- [ ] **Step 5: Commit**

```bash
git add src/oa_knowledge/production_pipeline.py src/oa_knowledge/web webui/src/App.tsx tests/test_production_pipeline.py tests/test_web_minimal_contract.py tests/test_web_v2_contract.py tests/test_frontend_v2_assets.py
git commit -m "feat: run only the minimal OARadar flows"
```

### Task 6: Add review-first cleanup status and safe migration commands

**Files:**
- Create: `src/oa_knowledge/data_migration.py`
- Modify: `src/oa_knowledge/data_governance/inventory.py`
- Modify: `src/oa_knowledge/data_governance/quarantine.py`
- Modify: `src/oa_knowledge/web/data_governance_views.py`
- Modify: `src/oa_knowledge/web/console_views.py`
- Modify: `src/oa_knowledge/cli.py`
- Test: `tests/test_data_migration.py`
- Modify: `tests/test_data_governance_inventory.py`, `tests/test_data_governance_quarantine.py`, `tests/test_cli.py`

**Interfaces:**
- Produces `OriginalInventory(ready, missing, mismatched, depth_limited, bytes)` and `build_original_inventory(settings, session) -> OriginalInventory`.
- Produces `build_migration_plan(settings) -> MigrationPlan`, `execute_migration(plan, *, execute: bool) -> MigrationSummary`, and `rollback_migration(plan) -> None`.
- Produces `oa data review <run-id>` and `oa data migrate --config config.yaml` dry-run commands; `--execute --confirmation MIGRATE-DATA-<plan-id>` is required for live cutover.

- [ ] **Step 1: Write failing migration tests with synthetic data only**

```python
def test_dry_run_never_creates_or_renames_live_data(tmp_path, settings, session):
    plan = build_migration_plan(settings)
    summary = execute_migration(plan, execute=False)
    assert summary.changed is False
    assert settings.data_root.exists()

def test_execute_hard_links_only_verified_originals_and_rolls_back_on_smoke_failure(tmp_path, settings, session):
    plan = build_migration_plan(settings)
    with pytest.raises(MigrationSmokeError):
        execute_migration(plan, execute=True, smoke=lambda: False)
    assert legacy_marker(settings.data_root).read_text() == "live"

def test_review_reports_only_aggregate_cleanup_information(runner, cleanup_run):
    result = runner.invoke(app, ["data", "review", str(cleanup_run.id)])
    assert "relative_path" not in result.stdout
```

- [ ] **Step 2: Run tests and confirm they fail because migration and review commands are absent**

Run: `uv run pytest tests/test_data_migration.py tests/test_data_governance_inventory.py tests/test_data_governance_quarantine.py tests/test_cli.py -v`

- [ ] **Step 3: Implement guarded commands**

Use `sqlite3.Connection.backup` to state-root private backups, a mode-0700 staging parent outside `data/`, `os.link` with verified-copy fallback, item-by-item validation, and same-filesystem `os.replace` directory renames. Refuse dirty target roots, symlinks, unknown data top-level entries, source hash changes, cross-device switches, or an incomplete inventory. Never call OA, Feishu, Qwen, or MinerU. The review command and dashboard return only aggregate cleanup data; Feishu reminder integration uses a once-only database fact and no business content.

- [ ] **Step 4: Run focused migration/governance tests**

Run: `uv run pytest tests/test_data_migration.py tests/test_data_governance_inventory.py tests/test_data_governance_quarantine.py tests/test_cli.py -v`

- [ ] **Step 5: Commit**

```bash
git add src/oa_knowledge/data_migration.py src/oa_knowledge/data_governance src/oa_knowledge/web src/oa_knowledge/cli.py tests/test_data_migration.py tests/test_data_governance_inventory.py tests/test_data_governance_quarantine.py tests/test_cli.py
git commit -m "feat: migrate OARadar data with review-first cleanup"
```

### Task 7: Update deployment paths, documentation, and end-to-end synthetic coverage

**Files:**
- Modify: `scripts/deploy-local.sh`
- Modify: `scripts/healthcheck.sh`
- Modify: `scripts/install-systemd-user.sh`
- Modify: `docs/runbook-oaradar-ops.md`
- Modify: `README.md`
- Modify: `README.zh-CN.md`
- Create: `scripts/smoke-minimal-data.sh`
- Test: `tests/test_minimal_data_e2e.py`
- Modify: `tests/test_deploy_local.py`, `tests/test_systemd_render.py`, `tests/test_public_release.py`

**Interfaces:**
- Produces a synthetic end-to-end run from verified originals to classified Markdown, then confirms no runtime artifacts are below data.
- Produces deployment units pointing browser/cache/state variables to XDG paths.
- Produces local `smoke-minimal-data.sh` with no OA, Feishu, or model invocation.

- [ ] **Step 1: Write the failing synthetic end-to-end test**

```python
def test_minimal_data_rebuild_keeps_only_originals_and_markdown(tmp_path, fake_classifier):
    fixture = seed_verified_done_originals(tmp_path)
    run_minimal_rebuild(fixture, classifier=fake_classifier)
    assert sorted(path.name for path in fixture.data_root.iterdir()) == ["markdown", "originals"]
    assert all_valid_original_hashes(fixture)
    assert classified_indexes_exist(fixture)
```

- [ ] **Step 2: Run test and confirm it fails because the complete minimal rebuild is absent**

Run: `uv run pytest tests/test_minimal_data_e2e.py -v`

- [ ] **Step 3: Implement deployment/docs/smoke coverage**

Render state and cache roots in user units, remove legacy `data/runtime` assumptions, and update operational documentation to require dry run, aggregate review, explicit migration confirmation, post-switch smoke, and separate original-retention verification. The smoke script creates temporary synthetic data only, runs validation without external integrations, and asserts exactly the two final data directories.

- [ ] **Step 4: Run final automated verification**

Run: `uv run python scripts/check_public_release.py`

Run: `uv run pytest`

Run: `cd webui && npm ci && npm run check && npm run build`

Run: `uv run pytest tests/test_minimal_data_e2e.py tests/test_web_minimal_contract.py tests/test_web_security.py tests/test_deploy_local.py tests/test_systemd_render.py -v`

- [ ] **Step 5: Commit**

```bash
git add scripts docs README.md README.zh-CN.md tests/test_minimal_data_e2e.py tests/test_deploy_local.py tests/test_systemd_render.py tests/test_public_release.py
git commit -m "docs: operate OARadar with minimal data storage"
```

### Task 8: Execute the real local migration only after code gates pass

**Files:**
- No tracked files unless an error exposes a missing redacted test case or runbook correction.
- Create only mode-0700 state-root backups and ignored private migration reports.

**Interfaces:**
- Consumes `oa data migrate`, `oa data review`, service units, and completed automated gates.
- Produces an aggregate user-facing validation report; no OA network action occurs.

- [ ] **Step 1: Verify clean release candidate and active unit names**

Run: `git status --short --branch && git diff --check`

Run: `systemctl --user is-active oaradar-web.service oaradar-worker.service oaradar-markdown-worker.service oaradar-hourly.timer oaradar-nightly.timer`

- [ ] **Step 2: Run read-only inventory and migration dry run**

Run: `uv run oa data review 1 --config config.yaml`

Run: `uv run oa data migrate --config config.yaml`

Expected: aggregate counts only; no OA title/path; no files renamed or deleted.

- [ ] **Step 3: Require all original evidence to be ready**

Run: `uv run oa data migrate --config config.yaml --validate-only`

Expected: zero missing, zero hash mismatch, zero unsafe path, and all depth-limit items explicitly incomplete. If any original is not ready, stop without executing migration and report aggregate error codes.

- [ ] **Step 4: Execute explicit local cutover and smoke**

Run: `uv run oa data migrate --config config.yaml --execute --confirmation MIGRATE-DATA-<plan-id>`

Expected: service stop, external database backup, hard-link/copy verification, same-filesystem directory swap, service restart, local smoke pass, and aggregate report only.

- [ ] **Step 5: Verify final data layout and clean eligible quarantine**

Run: `find data -mindepth 1 -maxdepth 1 -printf '%f\n' | sort`

Expected: exactly `markdown` and `originals`.

Run: `uv run oa data purge 1 --confirmation PURGE-CLEANUP-RUN-1 --config config.yaml`

Expected: only the previously reviewed temporary/rebuildable quarantine is deleted; no original is targeted.

- [ ] **Step 6: Report evidence**

Report only aggregate original count/bytes, Markdown success/unsupported/retryable failure counts, classification counts, validation result, data top-level directory list, and whether the old temporary quarantine was permanently removed.

## Plan self-review

Coverage: Task 1 implements the XDG/data contract; Task 2 the original/temporary parsing boundary; Tasks 3-4 bounded Qwen classification and categorized Markdown; Task 5 the three-flow worker/API surface; Task 6 guarded review/migration; Task 7 deployment and full synthetic verification; Task 8 real local migration and cleanup. Every specification section is covered.

No-placeholder check: all tasks declare files, interfaces, an executable failing test, expected verification commands, and commit scope. Names introduced by a task are defined in its interface before a later task consumes them.
