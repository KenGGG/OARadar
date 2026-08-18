# 存量知识统一流水线实施计划

> **代理执行要求：** 必须使用 `superpowers:executing-plans` 逐项执行；步骤使用复选框跟踪。禁止提交或推送。

**目标：** 把已办下载、唯一 Source Markdown、Curated 编目和发布串成可暂停、可恢复的持久流水线。

**架构：** 有效 `ParseArtifact` 是唯一 Source Markdown；`workspace/raw` 和 Curated 都从它发布。现有 `PipelineTask` 增加 `source_publish` 与 `curation` 阶段，全量编目由持久化任务分批调度，CLI 和 Web 共用服务。

**技术栈：** Python 3.12、SQLAlchemy、Typer、pytest

**设计说明：** `docs/superpowers/specs/2026-08-16-business-pipelines-webui-data-governance-design.md`

## 全局约束

- 原始已办和有效 ParseArtifact 不可由流水线改写。
- Source Markdown 镜像不得再次解析原始文件。
- qwen3.5:9b 只返回结构化决策，正文从 Source Markdown 确定性复制。
- 全量任务按项提交、可暂停续跑，深度达到 10 的事项不得完成。

---

### 任务 1：从有效 ParseArtifact 发布 Source Markdown

**文件：**
- 新建：`src/oa_knowledge/source_markdown/service.py`
- 修改：`src/oa_knowledge/markdown_export/service.py`
- 测试：`tests/test_source_markdown_service.py`

**接口：**
- 产出：`publish_active_artifact(session, settings, source_file_id: int) -> MarkdownExport`

- [ ] 测试发布内容来自 `data/parse/{output_relpath}`，包含来源元数据，且不调用 `parse_file`。
- [ ] 测试相同来源/产物哈希跳过，产物变化原子替换，缺少有效产物明确失败。
- [ ] 运行测试确认接口缺失而失败。
- [ ] 实现 ParseArtifact → `workspace/raw/sources/oa/...` 发布，并保留现有 `MarkdownExport` 账本。
- [ ] 运行 Source Markdown、Markdown 队列和解析专项测试。

### 任务 2：扩展单事项已办流水线

**文件：**
- 修改：`src/oa_knowledge/web/worker.py`
- 修改：`src/oa_knowledge/production_pipeline.py`
- 修改：`tests/test_worker.py`、`tests/test_production_pipeline.py`

**接口：**
- 阶段：`done_capture_and_archive → attachment_inventory → parse → source_publish → curation → completed`。

- [ ] 编写失败测试，证明解析完成后先发布 Source Markdown，再进入 Curated；缺少有效解析时不得进入编目。
- [ ] 测试进程恢复后从当前阶段继续，阶段幂等且不重复调用模型。
- [ ] 实现 `_pipeline_source_publish` 和 `_pipeline_curation`，复用现有 `run_curation(..., oa_item_key=...)`。
- [ ] 移除这条业务路径对旧 `ollama_extract` 的依赖，但保留旧任务兼容处理。
- [ ] 运行 Worker、生产流水线、编目和 Markdown 回归测试。

### 任务 3：建立持久化全量编目任务

**文件：**
- 新建：`src/oa_knowledge/curation/campaign.py`
- 修改：`src/oa_knowledge/web/worker.py`
- 修改：`src/oa_knowledge/cli.py`
- 测试：`tests/test_curation_campaign.py`

**接口：**
- 产出：`create_curation_campaign(settings, engine, *, mode: str, limit: int | None) -> OperationJob`
- 任务类型：`curation_sample`、`curation_full`、`curation_validate`。

- [ ] 测试样本最多 10 条、全量逐项创建/复用 PipelineTask、暂停后不领取新项、恢复后跳过成功签名、单项失败隔离。
- [ ] 运行测试确认任务类型尚不支持而失败。
- [ ] 实现任务创建、Worker 调度、心跳、进度和汇总；每次只运行一个本地模型事项。
- [ ] CLI 增加 `oa curate start-sample/start-all/pause/resume/status`，原同步 `run` 保留为诊断入口。
- [ ] 运行编目活动和 CLI 测试。

### 任务 4：统一业务阶段聚合

**文件：**
- 新建：`src/oa_knowledge/knowledge_status.py`
- 测试：`tests/test_knowledge_status.py`

**接口：**
- 产出：`knowledge_item_status(session, oa_item: OAItem) -> KnowledgeStageStatus`
- 产出字段：`discovery/download/archive/markdown/curation/publication/attention_codes`。

- [ ] 使用合成事项覆盖成功、下载失败、解析失败、缺少有效产物、低置信复核、深度上限和已发布。
- [ ] 运行测试确认聚合器缺失而失败。
- [ ] 从关系事实计算阶段，禁止只信任 `OAItem.pipeline_status`。
- [ ] 运行状态聚合、Web 现有已办列表和 Curated 校验测试。

### 任务 5：小样本质量门和真实持久任务

**文件：**
- 修改：`src/oa_knowledge/curation/campaign.py`
- 修改：`docs/runbook-oaradar-ops.md`
- 测试：`tests/test_curation_quality_gate.py`

- [ ] 测试未完成样本校验时拒绝启动全量；样本存在 `failed/needs_review/validate issue` 时保持锁定。
- [ ] 实现质量门和明确原因码。
- [ ] 在真实数据执行 10 条持久样本，运行 `validate/report`，报告只输出汇总。
- [ ] 样本通过后启动全量持久任务；若有待复核项，保持任务运行并在 WebUI 暴露人工队列。
