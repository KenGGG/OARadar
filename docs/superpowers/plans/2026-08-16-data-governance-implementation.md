# data 数据治理实施计划

> **代理执行要求：** 必须使用 `superpowers:executing-plans` 逐项执行；步骤使用复选框跟踪。当前任务禁止提交或推送，因此所有“提交”动作改为差异检查。

**目标：** 统一 `data_root` 路径语义，修复待办物理清理，并提供数据库感知的预检、隔离、恢复和永久清除能力。

**架构：** 所有数据库路径统一相对 `data_root`，由一个路径保护模块解析。数据治理服务先生成持久化、隐私安全的候选清单，再在同一文件系统内隔离；永久删除与恢复均以清单和二次校验为依据。原始已办和活动状态永远排除。

**技术栈：** Python 3.12、SQLAlchemy/Alembic、Typer、pytest、FastAPI

**设计说明：** `docs/superpowers/specs/2026-08-16-business-pipelines-webui-data-governance-design.md`

## 全局约束

- OA 只读，原始已办不得删除、覆盖或移动到不可恢复位置。
- 活动数据库、凭据、认证会话、最小审计/幂等账本不得清理。
- 所有路径相对 `data_root`；拒绝绝对路径、`..`、符号链接逃逸和预检后变化。
- 大体量候选先隔离，只有缓存等明确可重建内容允许直接清理。
- 测试只使用合成数据；状态和报告不得包含 OA 标题或正文。

---

### 任务 1：建立统一受保护路径解析器

**文件：**
- 新建：`src/oa_knowledge/storage_paths.py`
- 测试：`tests/test_storage_paths.py`

**接口：**
- 产出：`resolve_data_path(data_root: Path, relpath: str, *, allowed_prefixes: tuple[str, ...]) -> Path`
- 产出：`relative_data_path(data_root: Path, path: Path) -> str`

- [ ] 编写失败测试，覆盖 legacy `raw/pending/a.bin`、canonical `archive/raw/oa/pending/a.bin`、绝对路径、`..` 和符号链接逃逸。
- [ ] 运行 `uv run pytest tests/test_storage_paths.py -q`，确认新接口缺失而失败。
- [ ] 实现 `Path.resolve()` 后的共同父目录校验，并要求规范相对路径字符串与允许前缀匹配。
- [ ] 再次运行测试并执行 `git diff --check`。

### 任务 2：修复待办 sent-only 物理清理

**文件：**
- 修改：`src/oa_knowledge/pending_cleanup.py`
- 修改：`tests/test_pending_cleanup.py`

**接口：**
- 消费：`resolve_data_path(settings.data_root, file.local_relpath, allowed_prefixes=("raw/pending", "archive/raw/oa/pending"))`
- 产出：物理删除失败时 `cleanup_status=cleanup_failed`，不得删除数据库文件行或标记 `cleaned`。

- [ ] 增加两个失败测试：`local_relpath="raw/pending/..."` 和 `local_relpath="archive/raw/oa/pending/..."` 都必须删除真实文件；模拟 `Path.unlink` 失败时保留数据库关系并进入 `cleanup_failed`。
- [ ] 运行 `uv run pytest tests/test_pending_cleanup.py -q`，确认现有重复拼接路径行为失败。
- [ ] 将 `_delete_archived_file` 改为先解析并删除物理文件，成功后再删除 ORM 关系；不再使用 `settings.archive_root / local_relpath`。
- [ ] 运行待办清理、飞书和流水线专项测试。

### 任务 3：持久化清理计划和隔离清单

**文件：**
- 修改：`src/oa_knowledge/db/models.py`
- 新建：`src/oa_knowledge/db/migrations/versions/0031_data_governance.py`
- 新建：`src/oa_knowledge/data_governance/models.py`
- 测试：`tests/test_data_governance_models.py`

**接口：**
- 产出：`CleanupRun`（状态、规则版本、候选/隔离/释放统计、时间）。
- 产出：`CleanupItem`（相对路径、类别、大小、预检哈希、状态、隔离路径、原因码）。

- [ ] 编写迁移和约束失败测试：清理项路径必须相对、同一运行内路径唯一、状态限定为 `planned/quarantined/restored/purged/skipped/failed`。
- [ ] 运行模型和迁移测试，确认表不存在而失败。
- [ ] 实现模型及 `0030_curated_knowledge → 0031_data_governance` 前向迁移。
- [ ] 验证空库升级、现有库升级和重复初始化。

### 任务 4：实现隐私安全清理预检

**文件：**
- 新建：`src/oa_knowledge/data_governance/inventory.py`
- 新建：`src/oa_knowledge/data_governance/service.py`
- 测试：`tests/test_data_governance_inventory.py`

**接口：**
- 产出：`build_cleanup_plan(settings, engine, categories: set[str]) -> CleanupPlanSummary`
- 类别：`browser_cache`、`runtime_reports`、`expired_backups`、`sent_pending_orphans`、`rebuildable_projection`、`unreferenced_legacy`。

- [ ] 编写合成测试，证明原始已办、活动 DB/WAL/SHM、Cookies/Local Storage/Sessions、活动任务输入、哈希异常和复核项永不成为候选。
- [ ] 编写测试，证明浏览器 Cache/Code Cache、过期备份和已 sent 的无引用 pending 文件进入候选，报告只含类别/数量/字节。
- [ ] 运行测试确认服务缺失而失败。
- [ ] 实现目录清点、数据库引用集合、活动租约/任务保护、备份“最近两份＋周基线”规则和候选持久化。
- [ ] 运行数据治理专项测试。

### 任务 5：实现隔离、恢复和永久清除

**文件：**
- 新建：`src/oa_knowledge/data_governance/quarantine.py`
- 测试：`tests/test_data_governance_quarantine.py`

**接口：**
- 产出：`quarantine_run(settings, engine, run_id: int) -> CleanupExecutionSummary`
- 产出：`restore_run(settings, engine, run_id: int) -> CleanupExecutionSummary`
- 产出：`purge_run(settings, engine, run_id: int, *, confirmation: str) -> CleanupExecutionSummary`

- [ ] 测试同文件系统原子移动、目标冲突拒绝、预检哈希变化跳过、恢复不覆盖、隔离期未满拒绝永久删除。
- [ ] 运行测试确认接口缺失而失败。
- [ ] 在 `data/quarantine/{run_id}/` 内实现原子移动和 0600 清单；清单不保存 OA 标题或正文。
- [ ] 实现恢复和带精确确认串的永久删除，并记录释放字节。
- [ ] 运行专项测试和路径逃逸测试。

### 任务 6：提供 CLI/API 与真实数据第一轮安全清理

**文件：**
- 修改：`src/oa_knowledge/cli.py`
- 新建：`src/oa_knowledge/web/data_governance_views.py`
- 修改：`src/oa_knowledge/web/app.py`
- 测试：`tests/test_data_governance_cli.py`、`tests/test_data_governance_web.py`

**接口：**
- CLI：`oa data status/plan/quarantine/restore/purge`。
- API：`GET /api/data-governance`、`POST /api/data-governance/plans`、`POST /api/data-governance/runs/{id}/{action}`。

- [ ] 测试 CSRF、确认串、隐私安全响应和运行状态。
- [ ] 实现 CLI/API，并确保 Web 请求只创建持久任务，不执行长时间文件操作。
- [ ] 在真实 `data/` 上执行只读 `oa data plan`，核对预计空间和保护项。
- [ ] 服务停止时直接清理已确认的 Chrome Cache/Code Cache；对待办残留和备份只执行小样本隔离。
- [ ] 运行 `oa doctor`、数据库完整性、待办清理回归和空间复核；输出仅含汇总。
