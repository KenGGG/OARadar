# 原始已办线上逐项核验实施计划

> **代理执行要求：** 必须使用 `superpowers:executing-plans` 逐项执行；步骤使用复选框跟踪。禁止提交或推送。

**目标：** 以 OA 只读方式逐项验证全部已办事项和本地原件覆盖，缺失项进入安全补下载队列。

**架构：** 扩展现有 `OnlineAuditRun/OnlineAuditItem/Event`，复用 OA 浏览器、附件清点和下载代码。核验任务按项提交，支持暂停、恢复、限速和登录恢复；本地多出资料只进入复核，不自动删除。

**技术栈：** Python 3.12、Playwright、SQLAlchemy/Alembic、pytest

**设计说明：** `docs/superpowers/specs/2026-08-16-business-pipelines-webui-data-governance-design.md`

## 全局约束

- OA 只读；核验不得触发审批、回复、删除、转发或状态变化。
- 容器树最多 10 层；达到上限记录 `depth_limit_reached`。
- 线上没有哈希时不得宣称字节一致，只报告清单/下载完整性一致。
- 本地有、线上无法确认的原件进入复核，不自动删除。

---

### 任务 1：扩展核验观察模型

**文件：**
- 修改：`src/oa_knowledge/db/models.py`
- 新建：`src/oa_knowledge/db/migrations/versions/0032_online_verification.py`
- 修改：`src/oa_knowledge/online_audit.py`
- 测试：`tests/test_online_verification_models.py`

**接口：**
- `OnlineAuditItem` 增加正文/附件/容器数量、核验级别、补下载状态、最近观察哈希和 `depth_limit_reached`。

- [x] 编写迁移、状态约束和文本标识测试。
- [x] 运行测试确认字段不存在而失败。
- [x] 实现 `0032_online_evidence` 与 `0033_online_attachment_evidence` 迁移和结构化观察对象。
- [x] 运行模型及迁移测试。

### 任务 2：实现全量清单基线与逐项核验

**文件：**
- 修改：`src/oa_knowledge/online_audit.py`
- 修改：`src/oa_knowledge/web/worker.py`
- 测试：`tests/test_online_verification.py`

**接口：**
- 产出：`create_full_verification_run(settings, engine) -> OnlineAuditRun`
- 产出：`verify_one_item(...)-> VerificationObservation`

- [x] 使用伪 OA 适配器测试线上新增、本地缺失、完全匹配、本地多出、访问失败和深度上限。
- [x] 测试每项提交、暂停停止领取、过期租约恢复和限速调用。
- [x] 实现完整 Done 清单同步后生成核验项，并复用只读详情适配器执行逐附件清点。
- [x] 将明确缺失项幂等加入 `realtime_done` 补下载队列；其他差异进入复核。
- [x] 运行在线审计、调度和 OA 只读回归测试。

### 任务 3：调查哈希与历史清单异常

**文件：**
- 新建：`src/oa_knowledge/integrity_reconciliation.py`
- 修改：`src/oa_knowledge/web/data_governance_views.py`
- 测试：`tests/test_integrity_reconciliation.py`、`tests/test_data_governance_web.py`

**接口：**
- 产出：`classify_integrity_issues(settings, engine) -> IntegritySummary`
- 原因：`content_changed`、`stale_recorded_hash`、`manifest_schema_drift`、`real_missing_source`、`review_required`。

- [x] 用合成文件测试哈希异常冻结、清单旧 Schema 漂移和真实缺失的区分。
- [x] 实现只读分类，不自动重写 6 个异常文件或删除目录。
- [x] 对真实 6 个哈希异常和 2649 个清单差异运行汇总分类；报告不含路径或标题。
- [ ] 只有可证明为账本漂移的清单才允许从数据库事实重新生成，并保留旧清单隔离副本。

2026-08-16 真实脱敏结果：6 个哈希异常均为 `content_changed`，继续冻结；2,649 个
清单差异中 2,143 个为 `manifest_schema_drift`，506 个为 `review_required`，未发现
`real_missing_source`。结果仅以聚合计数写入 `runs` 审计账本并展示于“数据治理”；
当前不自动重写任何历史清单。

### 任务 4：提供核验 CLI/API 与持久部署

**文件：**
- 修改：`src/oa_knowledge/cli.py`
- 修改：`src/oa_knowledge/web/app.py`
- 新建：`src/oa_knowledge/web/verification_views.py`
- 测试：`tests/test_online_verification_web.py`

**接口：**
- CLI：`oa verify start/status/pause/resume/retry`。
- API：`GET /api/verification`、`POST /api/verification/{action}`。

- [x] 测试持久任务、CSRF、无正文响应和危险动作限制。
- [x] 实现 CLI/API，Web 只创建/控制任务，不在请求中直接访问 OA。
- [x] 小样本核验通过后启动全量持久核验；验证 Worker 重启后继续。
- [x] 使用 Web/CLI 观察真实覆盖率、匹配、补下载、复核和失败汇总。

实际接口集中在 `web/app.py` 与 `online_audit.py`，没有再建只作转发的
`verification_views.py`。2026-08-16 已启动 8,053 项持久核验；受控重启后从已提交进度继续。

### 任务 5：核验后的安全路径迁移门禁

**文件：**
- 新建：`src/oa_knowledge/archive_migration_campaign.py`
- 修改：`src/oa_knowledge/archive_reconciliation.py`
- 修改：`src/oa_knowledge/production_pipeline.py`
- 修改：`src/oa_knowledge/web/worker.py`
- 测试：`tests/test_archive_migration.py`、`tests/test_worker.py`、`tests/test_production_pipeline.py`

- [x] 未完成最新全量核验时不创建迁移任务。
- [x] 只选择完全一致/历史保留且未触发深度上限的 legacy 已办。
- [x] 迁移前后验证目录结构与文件字节指纹，拒绝符号链接、跨文件系统和目标冲突。
- [x] 活动 Markdown/流水线读取未退出时暂停并等待；按 25 项断点续跑。
- [x] 历史知识任务只领取 canonical 路径事项，差异原件保持原位待复核。
- [x] WebUI 数据治理页展示无 OA 标识的聚合迁移进度。
- [x] 补下载任务阻止迁移；成功归档后自动重开对应事项和审计进行再次取证。
- [x] 运行中动态纳入新增清单，重新归档使旧证据失效；实时队列抢占迁移。
- [x] 核验单项使用专用 120/30 秒超时，避免常规归档 1500/300 秒超时阻塞定时待办扫描。
- [x] 修复附件/关联容器总截止时间失效；部分证据拒绝比对，访问失败自动持久重试一次。
- [x] 历史重建同时校验 canonical 路径与最新审计安全证据，拒绝 canonical 路径上的差异项。
- [x] 迁移收敛后将核验不安全或迁移失败的历史任务停放为不可自动重试的人工复核项；显式重建不能绕过门禁。
- [x] 后续核验与迁移重新证明事项安全时，仅对该事项自动解除复核门禁并从头重建。
- [x] 更新核验一旦创建或重开，旧的已完成证据立即失效；历史任务只接受数据库中最新核验运行的完成证据。
- [x] 迁移要求审计时间窗内的全页清单对账，拒绝仅有头部增量的过期覆盖证据。
- [x] 夜间全页刷新建立全量版本签名，并仅为新建、变化和失败重试创建幂等归档任务。
