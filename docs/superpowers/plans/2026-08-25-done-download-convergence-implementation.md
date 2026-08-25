# 已办全量下载收敛实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**目标：** 修复已办全量下载的恢复、浏览器中断、归档路径碰撞和总览进度口径，并安全创建可恢复的本地下载任务。

**架构：** 重试任务的进度和待处理目标从不可变目标 key 集合以及清单行状态/时间戳重新计算，不以整数切片恢复。碰撞修复只在 `data/originals` 中建立确定性唯一子目录，并将哈希不一致事项送回正常只读下载流程。总览显示下载与 Markdown 的独立事实。

**技术栈：** Python 3.12、Typer、SQLAlchemy、SQLite、FastAPI、React/TypeScript、pytest。

**设计：** `docs/superpowers/specs/2026-08-25-done-download-convergence-design.md`

## 全局约束

- OA 集成只读；不得审批、回复、删除、转发或修改 OA 记录。
- `data/` 只允许产品根目录 `originals/` 和 `markdown/`；不新增第三个根目录。
- 所有测试使用合成或不可逆脱敏夹具；不得提交真实 OA 内容、数据、日志、浏览器资料或凭据。
- 数据库归档路径必须相对 `data_root`；容器深度上限仍为 10。
- 碰撞修复不删除历史共享目录；定时器和 Markdown worker 不在本次范围内。

---

### Task 1: 可恢复的清单重试协议

**文件：**

- 修改：`src/oa_knowledge/web/worker.py:1401-1520`
- 修改：`src/oa_knowledge/cli.py:645-855`
- 测试：`tests/test_worker.py`
- 测试：`tests/test_cli.py`

**接口：**

- 新增 worker 私有方法 `_manifest_retry_snapshot(job_id: int) -> ManifestRetrySnapshot`，返回目标总数、成功数、失败数、未尝试 key 和本轮浏览器中断次数。
- 新增 CLI 常量 `SYSTEMIC_BROWSER_CLOSED_EXIT = 5`；浏览器目标关闭时以此退出。
- `_execute_full_manifest_retry(job_id: int)` 以每批最多 100 个 key 运行子命令，且仅在所有目标成功时写入 `completed`。

- [ ] **Step 1: 写入恢复不按数字切片的失败测试**

```python
def test_manifest_retry_snapshot_keeps_unattempted_target_before_saved_progress(tmp_path: Path) -> None:
    # 第一个目标已终态，第二个 pending_download，第三个已失败。
    # 即使 job.progress_current 为 2，第二个仍必须出现在 pending_keys。
    assert snapshot.pending_keys == ["synthetic-pending"]
```

- [ ] **Step 2: 运行测试确认失败**

运行：`uv run pytest tests/test_worker.py -k manifest_retry_snapshot -v`

预期：因 `ManifestRetrySnapshot` / `_manifest_retry_snapshot` 尚不存在而失败。

- [ ] **Step 3: 实现基于数据库事实的快照**

在 `OperationWorker` 中定义只含聚合字段的 dataclass。读取 `parameters_json["oa_item_keys"]`，保留首次 `started_at`；将终态、开始后已尝试失败和仍未尝试目标分开统计。不得修改 `oa_item_keys` 或按 `progress_current` 切片。

- [ ] **Step 4: 运行恢复快照测试确认通过**

运行：`uv run pytest tests/test_worker.py -k manifest_retry_snapshot -v`

预期：PASS。

- [ ] **Step 5: 写入浏览器关闭的失败测试**

```python
def test_manifest_download_restores_current_row_and_stops_on_target_closed(...):
    # 用合成 BrowserSession 使第一个详情请求抛 TargetClosedError。
    # 当前行保留 pending_download、last_retry_at 恢复、后续行未改变，退出码为 5。
```

- [ ] **Step 6: 运行测试确认失败**

运行：`uv run pytest tests/test_cli.py -k target_closed -v`

预期：当前实现将事项记为下载失败并继续，断言失败。

- [ ] **Step 7: 实现系统性浏览器关闭退出**

在 `manifest_download` 中识别 `TargetClosedError` 及等价关闭错误；恢复当前行的处理前状态和时间戳后中止命令。普通详情/附件异常维持单项失败逻辑。

- [ ] **Step 8: 运行 CLI 回归测试确认通过**

运行：`uv run pytest tests/test_cli.py -k target_closed -v`

预期：PASS。

- [ ] **Step 9: 写入任务完成状态和分块运行的失败测试**

```python
def test_manifest_retry_marks_failed_when_all_targets_attempted_but_one_failed(...):
    # 模拟 101 项任务；worker 两次调用子命令，每批不超过 100。
    # 一项仍 download_failed 时，最终 OperationJob.status 必须为 failed。
```

- [ ] **Step 10: 运行测试确认失败**

运行：`uv run pytest tests/test_worker.py -k 'manifest_retry and failed' -v`

预期：现有 worker 把零退出子命令标记为 completed，断言失败。

- [ ] **Step 11: 实现分块、重算和有限浏览器重启**

每轮从 snapshot 取至多 100 个未尝试 key，启动新子命令；成功分块后重新计算；浏览器关闭最多连续重启三次；所有目标成功才完成，任何已尝试目标失败则以汇总错误码失败。保留 `started_at`，并把 `progress_current` 写为成功加失败数。

- [ ] **Step 12: 运行 Task 1 测试**

运行：`uv run pytest tests/test_worker.py tests/test_cli.py -v`

预期：PASS。

- [ ] **Step 13: 提交 Task 1**

```bash
git add src/oa_knowledge/web/worker.py src/oa_knowledge/cli.py tests/test_worker.py tests/test_cli.py
git commit -m "fix: make manifest retries resumable"
```

### Task 2: 碰撞安全的归档路径和本地修复

**文件：**

- 修改：`src/oa_knowledge/archive_paths.py`
- 修改：`src/oa_knowledge/detail_archive.py`
- 创建：`src/oa_knowledge/archive_collision_repair.py`
- 修改：`src/oa_knowledge/cli.py`
- 测试：`tests/test_archive_paths.py`
- 测试：`tests/test_archive_collision_repair.py`

**接口：**

- `collision_done_archive_directory(title: str, oa_item_key: str, initiated_at: datetime | None) -> PurePosixPath` 生成带 10 位 SHA-256 后缀的路径。
- `repair_archive_collisions(settings: Settings, *, dry_run: bool) -> CollisionRepairReport` 只复制校验通过的碰撞文件，并把不一致事项重置为 `download_failed`。
- 新增本地 CLI 子命令 `oa archive repair-collisions --dry-run` 与 `oa archive repair-collisions`。

- [ ] **Step 1: 写入唯一碰撞路径的失败测试**

```python
def test_collision_done_archive_directory_keeps_two_same_title_items_separate() -> None:
    first = collision_done_archive_directory("合成事项", "synthetic-key-a", datetime(2024, 1, 2))
    second = collision_done_archive_directory("合成事项", "synthetic-key-b", datetime(2024, 1, 2))
    assert first != second
    assert first.parts[:3] == ("originals", "2024", "01")
```

- [ ] **Step 2: 运行测试确认失败**

运行：`uv run pytest tests/test_archive_paths.py -k collision -v`

预期：导入失败，因为函数尚不存在。

- [ ] **Step 3: 实现确定性碰撞路径和下载时探测**

保留无歧义事项的历史目录格式。下载/重试前查询是否有其他 OA item 共用候选历史目录；仅碰撞时调用新路径函数。后缀不得暴露 OA 原始 key。

- [ ] **Step 4: 运行路径测试确认通过**

运行：`uv run pytest tests/test_archive_paths.py tests/test_detail_archive.py -v`

预期：PASS。

- [ ] **Step 5: 写入碰撞修复 dry-run 与幂等修复的失败测试**

```python
def test_repair_archive_collisions_copies_valid_file_and_marks_hash_mismatch(tmp_path: Path, config_file: Path) -> None:
    # 两个合成 OA item 共用旧目录；一个文件哈希匹配，一个不匹配。
    # dry-run 不改 DB/磁盘；执行后匹配项有唯一 relpath，不匹配清单为 download_failed。
```

- [ ] **Step 6: 运行测试确认失败**

运行：`uv run pytest tests/test_archive_collision_repair.py -v`

预期：模块不存在而失败。

- [ ] **Step 7: 实现仅碰撞目录的本地修复**

扫描重复 `archive_relpath` 的 done OA item。逐文件校验存在、大小、哈希；只原子复制通过校验的文件到 `data/originals` 内的新路径。全有效时更新相对路径；任一文件异常时不复制该事项并标记 `download_failed/local_verification`。不删除或覆盖旧目录。dry-run 只返回汇总。

- [ ] **Step 8: 增加 CLI 并运行修复测试**

运行：`uv run pytest tests/test_archive_collision_repair.py tests/test_archive_paths.py tests/test_cli_registration.py -v`

预期：PASS。

- [ ] **Step 9: 提交 Task 2**

```bash
git add src/oa_knowledge/archive_paths.py src/oa_knowledge/detail_archive.py src/oa_knowledge/archive_collision_repair.py src/oa_knowledge/cli.py tests/test_archive_paths.py tests/test_archive_collision_repair.py
git commit -m "fix: isolate colliding done archives"
```

### Task 3: 收敛的总览下载口径

**文件：**

- 修改：`src/oa_knowledge/web/simple_status.py:120-185`
- 修改：`webui/src/App.tsx`
- 测试：`tests/test_simple_status.py`
- 测试：`tests/test_frontend_v2_assets.py`

**接口：**

- `_done_summary()` 返回 `download_complete_items`、`waiting_download_items`、`download_failed_items`、`waiting_markdown_items` 和 `actual_download_queue_items`。
- `queued_items` 保留兼容，但文案仅表示等待 Markdown，不表示下载队列。

- [ ] **Step 1: 写入总览分离下载与 Markdown 的失败测试**

```python
def test_simple_status_exposes_download_backlog_separately_from_markdown_readiness(config_file: Path) -> None:
    # 构造 pending_download、download_failed、downloaded 且未索引的合成清单行。
    assert done["waiting_download_items"] == 1
    assert done["download_failed_items"] == 1
    assert done["waiting_markdown_items"] == 1
    assert "等待 Markdown" in done["headline"]
```

- [ ] **Step 2: 运行测试确认失败**

运行：`uv run pytest tests/test_simple_status.py -k backlog -v`

预期：新字段不存在，断言失败。

- [ ] **Step 3: 实现统计和最小 UI 文案调整**

从清单状态计算显式下载字段；真实队列只统计 queued/running 的 Done `PipelineTask` 或下载 `OperationJob`。保留旧字段兼容，将总览文案从“排队”改为“等待 Markdown”。前端仅替换展示标签和读取新字段，不改变导航或加入新工作流。

- [ ] **Step 4: 运行后端和前端定向测试**

运行：`uv run pytest tests/test_simple_status.py tests/test_frontend_v2_assets.py -v && cd webui && npm run build`

预期：PASS，构建退出码 0。

- [ ] **Step 5: 提交 Task 3**

```bash
git add src/oa_knowledge/web/simple_status.py webui/src/App.tsx tests/test_simple_status.py tests/test_frontend_v2_assets.py
git commit -m "fix: clarify done download progress"
```

### Task 4: 全面验证、部署和可观察恢复

**文件：**

- 修改：`docs/runbook-oaradar-ops.md`
- 测试：现有完整套件；不创建真实 OA 夹具。

**接口：**

- `oa archive repair-collisions --dry-run` 先返回只含聚合数据的报告。
- `oa archive repair-collisions` 完成本地修复后可由 `oa audit` 验证。

- [ ] **Step 1: 更新运行手册**

记录固定顺序：停止 OA worker、dry-run、执行碰撞修复、`oa audit`、创建当前待下载/失败事项的重试任务、重启 worker、观察任务快照。明确不启用 timer/Markdown worker，不删除旧目录。

- [ ] **Step 2: 运行全套测试和前端构建**

运行：`uv run pytest -q && cd webui && npm run build`

预期：pytest 全部通过，前端构建退出码 0。

- [ ] **Step 3: 在生产数据上执行只读预检**

运行：`uv run oa archive repair-collisions --dry-run --config config.yaml && uv run oa audit --config config.yaml`

预期：dry-run 只输出聚合数量；审计可显示当前哈希不一致数量但不改数据。

- [ ] **Step 4: 以可恢复方式执行生产本地修复和入队**

正常停止 worker；执行无 `--dry-run` 的碰撞修复；再次运行 `oa audit`；通过现有重试入口仅为当前 `pending_download` 与 `download_failed` 创建一个 `full_manifest_retry`。重新启动 worker，确认任务有心跳且 `progress_current` 不超过 `progress_total`。

- [ ] **Step 5: 提交 Task 4**

```bash
git add docs/runbook-oaradar-ops.md docs/superpowers/plans/2026-08-25-done-download-convergence-implementation.md
git commit -m "docs: document done download recovery"
```

