# WebUI 业务流水线重构实施计划

> **代理执行要求：** 必须使用 `superpowers:executing-plans` 逐项执行；步骤使用复选框跟踪。禁止提交或推送。

**目标：** 把 WebUI 重构为“总览 / 存量知识 / 待办提醒 / 数据治理 / 系统设置”，完整呈现两条业务流水线与安全操作入口。

**架构：** 后端提供按业务聚合的轻量 API；前端不推断阶段、不直接执行长任务，只创建持久任务并轮询状态。所有主文案使用中文，敏感正文仅在明确详情操作中按既有权限读取。

**技术栈：** FastAPI、React、TypeScript、Vite、pytest

**设计说明：** `docs/superpowers/specs/2026-08-16-business-pipelines-webui-data-governance-design.md`

## 全局约束

- 一级导航固定为：总览、存量知识、待办提醒、数据治理、系统设置。
- 页面响应默认不含 OA 正文、完整来源路径或凭据。
- 长任务只能通过持久化 OperationJob/PipelineTask 运行。
- 全量编目前必须完成 10 条样本质量门；清理必须预检和确认。

---

### 任务 1：建立业务聚合 API

**文件：**
- 新建：`src/oa_knowledge/web/knowledge_views.py`
- 新建：`src/oa_knowledge/web/data_governance_views.py`
- 新建：`src/oa_knowledge/web/verification_views.py`
- 修改：`src/oa_knowledge/web/app.py`
- 测试：`tests/test_business_web_api.py`

**接口：**
- `GET /api/overview`
- `GET /api/knowledge/items`、`GET /api/knowledge/status`
- `POST /api/knowledge/jobs/{action}`
- `GET /api/data-governance`
- `GET /api/verification`

- [ ] 编写失败测试，覆盖阶段计数、分页筛选、持久任务创建、CSRF、隐私字段排除和中文状态标签。
- [ ] 运行 API 测试确认路由缺失。
- [ ] 复用 `knowledge_status`、编目活动、核验和数据治理服务实现薄视图。
- [ ] 运行 Web API 和既有路由回归测试。

### 任务 2：重构导航与总览

**文件：**
- 修改：`webui/src/App.tsx`
- 修改：`webui/src/index.css`
- 测试：`webui/src/App.test.tsx`（若项目未配置前端测试，则由后端静态文案测试和 TypeScript 检查覆盖）

- [ ] 先更新后端静态页面测试，断言五个中文一级导航和旧“Markdown 输出”一级导航消失。
- [ ] 将 `View` 改为 `overview | knowledge | pending | governance | settings`。
- [ ] 总览展示存量知识和待办提醒两张主流程卡，以及核验/清理/人工复核告警。
- [ ] 运行 `npm run check` 和相关 Web 测试。

### 任务 3：实现“存量知识”页面

**文件：**
- 新建：`webui/src/views/KnowledgeView.tsx`
- 修改：`webui/src/App.tsx`
- 修改：`webui/src/index.css`

- [ ] 定义 TypeScript 类型，覆盖业务阶段、活动任务、样本质量门和在线核验汇总。
- [ ] 实现指标、搜索筛选、六阶段状态条、任务进度、暂停/继续/重试/校验按钮。
- [ ] “启动全量”在样本门未通过时禁用，并显示中文原因；确认框明确任务长期运行且只读访问 OA。
- [ ] 实现事项详情的原件、Source Markdown、Curated 与来源关系摘要，不默认显示正文。
- [ ] 运行 TypeScript 检查、生产构建和 Web API 测试。

### 任务 4：重构“待办提醒”页面

**文件：**
- 新建：`webui/src/views/PendingView.tsx`
- 修改：`webui/src/App.tsx`

- [x] 把现有待办表和详情迁入新组件，展示“发现/下载/MD 化/摘要/飞书/清理”六步状态。
- [x] 为 `unknown_outcome`、摘要失败、飞书失败和清理失败提供显著中文提示。
- [x] 保留安全重试；未知发送结果不得显示自动重发按钮，普通 API 重试同样拒绝未知结果。
- [x] 运行前端检查/构建和待办 Web 回归测试。

### 任务 5：实现“数据治理”页面

**文件：**
- 新建：`webui/src/views/DataGovernanceView.tsx`
- 修改：`webui/src/App.tsx`
- 修改：`webui/src/index.css`

- [ ] 实现五类生命周期容量卡、保护状态、审计异常、备份、浏览器缓存和隔离运行列表。
- [ ] 实现“生成预检”“隔离候选”“恢复”和“永久清除”；每个动作显示预计字节、恢复能力和精确确认要求。
- [ ] 原始已办、活动数据库、哈希异常和运行中任务只显示保护标签，不提供删除控件。
- [ ] 运行前端检查/构建、CSRF 和数据治理 API 测试。

### 任务 6：系统设置、文档与部署验证

**文件：**
- 修改：`webui/src/App.tsx`
- 修改：`README.zh-CN.md`
- 修改：`docs/runbook-oaradar-ops.md`
- 修改：`scripts/healthcheck.sh`

- [ ] 把模型、飞书、定时器和高级服务操作集中到系统设置；移除重复技术页入口。
- [ ] 运维手册用中文记录两条流水线、全量编目、在线核验、数据清理、隔离恢复和回滚。
- [ ] 健康检查增加编目任务、核验任务、隔离运行、待办清理失败和数据保护异常汇总。
- [ ] 运行完整 `pytest`、`npm run check && npm run build`、敏感信息发布检查、CLI/config/doctor、systemd 渲染和 `git diff --check`。
- [ ] 启动用户已授权的 Worker/定时器，确认持久任务可恢复；不发送飞书测试消息。
