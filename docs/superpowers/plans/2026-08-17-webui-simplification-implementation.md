# OARadar 极简 WebUI 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将 OARadar 默认 WebUI 缩减为“总览、已办资料、系统设置”，用真实业务结果回答已办知识库和待办飞书是否正常，同时保留折叠的高级维护能力。

**Architecture:** 后端新增一个脱敏的 `/api/simple-status` 聚合接口，并为已办列表计算单一简化状态；前端只渲染后端业务口径，不自行拼接数据库状态。现有复杂组件和 API 不删除，移动到“系统设置 → 高级维护”中。

**Tech Stack:** Python 3.12、FastAPI、SQLAlchemy、SQLite、React、TypeScript、Vite、pytest

**Spec:** `docs/superpowers/specs/2026-08-17-webui-simplification-design.md`

## Global Constraints

- OA 集成严格只读；不得审批、回复、删除、转发或修改 OA 线上记录。
- OA 内容、附件、凭据、浏览器配置和真实快照必须保持本机，不得写入测试夹具、日志、提交或外部服务。
- 不删除现有 API、数据库表、高级维护能力或用户已有未提交修改。
- 不新增数据库迁移；所有状态从现有事实表聚合。
- 默认一级导航必须只有：总览、已办资料、系统设置。
- 待办/已办增量扫描继续每小时 05 分；夜间全量扫描继续每日 23:30。
- 所有列表继续服务端分页，默认每页 50 项。
- OA 标识按文本处理；归档路径相对 `data_root`；十层深度上限不得误报完成。
- 不发送飞书测试消息，不启动新的全量在线核验，不执行数据清理。
- 当前工作树已有大量用户修改；实施模型不得提交、推送、重置或覆盖无关变更。

---

### Task 1: 建立极简状态聚合服务

**Files:**
- Create: `src/oa_knowledge/web/simple_status.py`
- Modify: `src/oa_knowledge/web/app.py`
- Test: `tests/test_simple_status.py`

**Interfaces:**
- Produces: `simple_status(settings: Settings) -> dict[str, Any]`
- Produces: `GET /api/simple-status`
- Consumes: 现有 `schedule_status(settings)`、`dashboard_status(settings)` 和 SQLAlchemy 事实表

- [ ] **Step 1: 编写接口隐私与结构失败测试**

在 `tests/test_simple_status.py` 使用现有 `config_file` 合成夹具创建数据库事实，测试：

```python
def test_simple_status_returns_plain_language_business_results_without_oa_content(config_file: Path) -> None:
    client = _client(config_file)
    payload = client.get("/api/simple-status").json()

    assert set(payload) == {"generated_at", "overall_status", "done", "pending", "oa_activity", "attention"}
    assert set(payload["done"]) >= {
        "status", "headline", "oa_total", "archive_complete", "excluded",
        "no_attachment", "markdown_ready_items", "published_items",
        "queued_items", "running_items", "failed_items", "review_items", "last_scan_at",
    }
    assert set(payload["pending"]) >= {
        "status", "headline", "frequency_text", "last_scan_at", "next_scan_at",
        "oa_pending_count", "model_name", "model_success", "model_fallback",
        "model_failed", "feishu_sent", "feishu_failed", "feishu_unknown",
        "last_feishu_success_at",
    }
    serialized = json.dumps(payload, ensure_ascii=False)
    assert "synthetic confidential title" not in serialized
    assert "payload_json" not in serialized
    assert "structured_json" not in serialized
```

- [ ] **Step 2: 运行测试并确认因路由缺失而失败**

Run:

```bash
uv run pytest tests/test_simple_status.py::test_simple_status_returns_plain_language_business_results_without_oa_content -q
```

Expected: `404` 或响应结构断言失败。

- [ ] **Step 3: 实现纯聚合模块**

`src/oa_knowledge/web/simple_status.py` 只负责计数和文案，不读取文件正文。至少拆分以下小函数：

```python
def _done_summary(session: Session, settings: Settings, schedule: dict) -> dict[str, Any]: ...
def _pending_summary(session: Session, settings: Settings, schedule: dict) -> dict[str, Any]: ...
def _overall_status(done: dict, pending: dict, oa_activity: dict) -> str: ...
def simple_status(settings: Settings) -> dict[str, Any]: ...
```

实现口径必须逐项遵守 spec 第 4、5 节：

- `markdown_ready_items` 使用关联 `OAItem → ArchivedFile → MarkdownExport` 的 distinct OAItem 数，且 `MarkdownExport.status == "success"`。
- `published_items` 使用最新 `CuratedRun`，要求运行状态 `completed`、至少一个 `CuratedDecision`，且全部决策为 `published`；按 logical item 去重。
- `model_success` 仅统计 `SummaryVersion.summary_kind == "pending"`、`status == "current"` 且 `model_name` 不是 `deterministic-fallback` 的记录。
- `model_fallback` 仅统计 `model_name == "deterministic-fallback"`。
- `model_failed` 统计待办 `SummaryJob` 的失败/重试终止状态和待办流水线中不可恢复的摘要失败，避免同一 logical item 重复计数。
- `feishu_unknown` 包括 `unknown`、`unknown_outcome` 等无法确认投递结果的状态。
- `frequency_text` 固定从当前已部署计划事实翻译为“每小时 05 分检查”，不得从某次运行时间猜测。
- `next_scan_at` 优先读取 systemd timer 状态；不可用时返回 `null`。

- [ ] **Step 4: 注册 GET 路由**

在 `src/oa_knowledge/web/app.py` 导入并注册：

```python
@app.get("/api/simple-status")
def get_simple_status() -> dict:
    return simple_status(settings)
```

- [ ] **Step 5: 补充业务口径测试**

新增以下独立测试，全部使用合成标题和不可逆假标识：

```python
def test_simple_status_does_not_count_markdown_as_final_publication(config_file: Path) -> None: ...
def test_simple_status_counts_only_all_published_curated_runs_as_complete(config_file: Path) -> None: ...
def test_simple_status_distinguishes_qwen_success_fallback_and_failure(config_file: Path) -> None: ...
def test_simple_status_reports_missing_schedule_time_as_unknown_not_zero(config_file: Path) -> None: ...
def test_simple_status_never_marks_depth_limit_reached_complete(config_file: Path) -> None: ...
```

- [ ] **Step 6: 运行后端测试**

Run:

```bash
uv run pytest tests/test_simple_status.py tests/test_console_views.py tests/test_schedule_web.py -q
```

Expected: PASS。

- [ ] **Step 7: 检查本任务差异，不提交**

Run:

```bash
git diff --check -- src/oa_knowledge/web/simple_status.py src/oa_knowledge/web/app.py tests/test_simple_status.py
```

Expected: 无输出。

---

### Task 2: 为已办事项提供单一简化状态

**Files:**
- Modify: `src/oa_knowledge/web/console_views.py`
- Modify: `src/oa_knowledge/web/app.py`
- Test: `tests/test_console_views.py`

**Interfaces:**
- Produces: `_simple_done_state(session, manifest, archived, markdown, stages) -> dict[str, str | None]`
- Extends: `GET /api/done-archives` query parameter `simple_status`
- Extends: 每个 `items[]` 返回 `simple_status`、`simple_status_label`、`attention_reason`、`updated_at`

- [ ] **Step 1: 编写状态优先级失败测试**

用参数化测试覆盖以下映射：

```python
@pytest.mark.parametrize(("facts", "expected"), [
    ({"manifest": "discovered"}, "waiting_download"),
    ({"manifest": "downloaded", "markdown": "pending"}, "waiting_markdown"),
    ({"manifest": "downloaded", "markdown": "success", "curation": "queued"}, "waiting_classification"),
    ({"manifest": "downloaded", "markdown": "success", "curation": "completed", "decisions": ["published"]}, "completed"),
    ({"manifest": "downloaded", "markdown": "success", "curation": "needs_review"}, "attention"),
    ({"manifest": "skipped"}, "excluded"),
    ({"manifest": "depth_limit_reached"}, "attention"),
])
def test_done_archive_simple_status_uses_business_priority(config_file: Path, facts: dict, expected: str) -> None:
    ...
```

- [ ] **Step 2: 运行测试确认新字段缺失**

Run:

```bash
uv run pytest tests/test_console_views.py -k simple_status -q
```

Expected: FAIL，原因是缺少简化状态或筛选参数。

- [ ] **Step 3: 实现状态计算**

在现有 `_done_pipeline_stages` 附近实现 `_simple_done_state`，但不要依赖前端六阶段颜色。错误原因使用现有中文映射：

```python
_SIMPLE_DONE_LABELS = {
    "waiting_download": "等待下载",
    "waiting_markdown": "等待 MD 化",
    "waiting_classification": "等待归类",
    "completed": "已完成",
    "attention": "需要处理",
    "excluded": "已按规则排除",
}
```

`attention_reason` 只返回简短中文原因；原始 `error_code` 可继续保留在高级接口，不放入默认表格。

- [ ] **Step 4: 将筛选下推到服务端正确分页**

不能先取 50 行再用 Python 过滤，否则页数和总数会错误。为 `simple_status` 构造 SQL 候选集合或先查询匹配 manifest ID 的子查询，然后再计算 `total`、排序、offset 和 limit。

请求示例：

```text
GET /api/done-archives?page=1&page_size=50&simple_status=attention
```

不在允许集合中的值返回 HTTP 422。

- [ ] **Step 5: 运行分页与回归测试**

Run:

```bash
uv run pytest tests/test_console_views.py tests/test_web.py -q
```

Expected: PASS，且既有 `archive_status`、`markdown_status`、`handoff_status` 查询仍兼容。

- [ ] **Step 6: 检查本任务差异，不提交**

Run:

```bash
git diff --check -- src/oa_knowledge/web/console_views.py src/oa_knowledge/web/app.py tests/test_console_views.py
```

Expected: 无输出。

---

### Task 3: 把前端应用拆成三个默认入口

**Files:**
- Create: `webui/src/types/simple-status.ts`
- Create: `webui/src/views/SimpleOverviewView.tsx`
- Create: `webui/src/views/SimpleDoneView.tsx`
- Create: `webui/src/views/SimpleSettingsView.tsx`
- Create: `webui/src/views/AdvancedMaintenance.tsx`
- Modify: `webui/src/App.tsx`
- Modify: `webui/src/styles.css`
- Test: `tests/test_web.py`

**Interfaces:**
- Consumes: `GET /api/simple-status`
- Consumes: extended `GET /api/done-archives`
- Produces: `type View = "overview" | "done" | "settings"`
- Produces: `<AdvancedMaintenance expanded={boolean} />`

- [ ] **Step 1: 写默认导航失败测试**

在后端静态资源测试中断言构建产物包含三个默认中文入口，并且旧入口不再作为一级导航：

```python
def test_webui_default_navigation_is_reduced_to_three_business_entries(client) -> None:
    html = client.get("/").text
    assert "root" in html
    js = _read_built_javascript()
    assert "总览" in js
    assert "已办资料" in js
    assert "系统设置" in js
    assert 'label:"待办提醒"' not in js
    assert 'label:"数据治理"' not in js
```

测试应匹配明确的导航对象片段，不能简单禁止整个 JS 出现“待办提醒”或“数据治理”，因为高级维护仍需要这些文案。

- [ ] **Step 2: 运行测试确认旧导航仍存在**

Run:

```bash
uv run pytest tests/test_web.py -k default_navigation -q
```

Expected: FAIL。

- [ ] **Step 3: 建立共享 TypeScript 类型**

在 `webui/src/types/simple-status.ts` 精确声明 spec 第 5 节响应结构，状态使用联合类型：

```typescript
export type BusinessTone = "normal" | "working" | "attention" | "fallback_used" | "unknown"
export type SimpleDoneState = "waiting_download" | "waiting_markdown" | "waiting_classification" | "completed" | "attention" | "excluded"
```

禁止 `any`。

- [ ] **Step 4: 缩减 App 导航和数据加载**

`App.tsx` 默认只注册：

```typescript
const NAV = [
  { id: "overview" as View, label: "总览", icon: LayoutDashboard },
  { id: "done" as View, label: "已办资料", icon: BookOpen },
  { id: "settings" as View, label: "系统设置", icon: SettingsIcon },
]
```

总览只请求 `/api/simple-status`；已办资料只请求分页 `/api/done-archives`；系统设置请求既有设置、调度和维护接口。保留 5 秒静默刷新，不重置当前搜索和分页。

- [ ] **Step 5: 把旧复杂组件移入高级维护**

将以下现有能力迁入 `AdvancedMaintenance.tsx`，不得复制一套新实现：

- `OnlineVerificationView`
- `SourceReviewQueue`
- `MarkdownOutputsView`
- 处理中心/任务队列摘要
- `GovernanceView`
- 运行维护操作

默认不请求这些重接口；只有用户第一次展开“高级维护”时再加载。折叠后可以保留已加载数据，但停止 5 秒轮询。

- [ ] **Step 6: 实现基础响应式样式**

在 `styles.css` 增加 `simple-*` 命名样式，不覆盖现有高级组件规则。要求：

- 桌面三张卡清晰分区；
- 低于 760px 时单列；
- 状态有文字和颜色双重表达；
- 高级维护折叠按钮可键盘操作并带 `aria-expanded`。

- [ ] **Step 7: 构建并使导航测试转绿**

Run:

```bash
cd webui
npm run check
npm run build
cd ..
uv run pytest tests/test_web.py -k default_navigation -q
```

Expected: 全部 PASS。

- [ ] **Step 8: 检查本任务差异，不提交**

Run:

```bash
git diff --check -- webui/src tests/test_web.py src/oa_knowledge/web/static
```

Expected: 无输出。

---

### Task 4: 实现大白话总览

**Files:**
- Modify: `webui/src/views/SimpleOverviewView.tsx`
- Modify: `webui/src/styles.css`
- Test: `tests/test_simple_status.py`
- Test: `tests/test_web.py`

**Interfaces:**
- Consumes: `SimpleStatusResponse`
- Produces: 已办知识库、待办飞书、OA 后台状态三张卡

- [ ] **Step 1: 增加文案状态测试**

后端测试至少覆盖：全部完成、仍在建设、使用兜底、明确失败、Worker 心跳未知五种情形。断言文案不能包含英文错误码。

- [ ] **Step 2: 运行测试确认边界文案尚未实现**

Run:

```bash
uv run pytest tests/test_simple_status.py -k headline -q
```

Expected: FAIL。

- [ ] **Step 3: 实现三张结果卡**

`SimpleOverviewView` 展示：

- 顶部总体结论；
- 已办卡：`headline`、同步/原件/MD/最终发布/排队五个数字、最近扫描、“查看已办资料”；
- 待办卡：`headline`、频率、最近/下次扫描、当前待办、飞书成功/失败、qwen 成功/兜底/失败；
- OA 卡：状态、当前动作、最后心跳。

数字为 0 也必须显示；字段为 `null` 时显示“尚未取得”，不得显示 0。

- [ ] **Step 4: 实现聚合注意事项**

只展示 `attention[]` 聚合项。每项包含中文标签、严重程度和目标入口；默认不展示 OA 标题清单。点击后跳转到已办筛选或展开高级维护。

- [ ] **Step 5: 运行后端、前端验证**

Run:

```bash
uv run pytest tests/test_simple_status.py tests/test_web.py -q
cd webui && npm run check && npm run build
```

Expected: PASS。

- [ ] **Step 6: 检查本任务差异，不提交**

Run:

```bash
git diff --check -- webui/src/views/SimpleOverviewView.tsx webui/src/styles.css tests/test_simple_status.py tests/test_web.py
```

Expected: 无输出。

---

### Task 5: 实现极简已办资料页

**Files:**
- Modify: `webui/src/views/SimpleDoneView.tsx`
- Modify: `webui/src/styles.css`
- Test: `tests/test_console_views.py`

**Interfaces:**
- Consumes: `GET /api/done-archives?page={page}&page_size=50&query={query}&simple_status={status}`
- Produces: 搜索、六种状态筛选、分页和简化详情

- [ ] **Step 1: 编写搜索和简化筛选分页测试**

创建超过 50 条合成事项，证明 `total`、页数和第二页数据基于完整筛选结果，而不是“先分页后过滤”。

- [ ] **Step 2: 运行测试确认当前筛选行为不满足要求**

Run:

```bash
uv run pytest tests/test_console_views.py -k 'simple_status and pagination' -q
```

Expected: FAIL。

- [ ] **Step 3: 实现简化表格与筛选**

默认只显示标题、办结时间、原件数量、当前状态、最后更新时间。状态筛选值固定为 Task 3 的 `SimpleDoneState`，搜索框占位文案为“搜索事项标题”。

- [ ] **Step 4: 实现简化详情抽屉**

点击整行打开抽屉，正文只回答：原件、Markdown、归类发布和异常原因。技术字段放进 `<details><summary>查看技术详情</summary>`，默认关闭。

“需要处理”事项只提供“打开高级维护”动作。不得在默认页提供删除、强制清理、在线 OA 修改或盲目重试全部按钮。

- [ ] **Step 5: 运行分页和前端验证**

Run:

```bash
uv run pytest tests/test_console_views.py -q
cd webui && npm run check && npm run build
```

Expected: PASS。

- [ ] **Step 6: 检查本任务差异，不提交**

Run:

```bash
git diff --check -- webui/src/views/SimpleDoneView.tsx webui/src/styles.css src/oa_knowledge/web/console_views.py tests/test_console_views.py
```

Expected: 无输出。

---

### Task 6: 简化设置并保留按需高级维护

**Files:**
- Modify: `webui/src/views/SimpleSettingsView.tsx`
- Modify: `webui/src/views/AdvancedMaintenance.tsx`
- Modify: `webui/src/styles.css`
- Test: `tests/test_provider_settings.py`
- Test: `tests/test_schedule_web.py`
- Test: `tests/test_web.py`

**Interfaces:**
- Consumes: 既有 `/api/settings`、`/api/schedule/status`、`/api/maintenance` 和高级只读接口
- Produces: 默认设置摘要及惰性加载高级维护区

- [ ] **Step 1: 增加设置摘要回归测试**

测试设置 API 能提供模型名、飞书是否已配置、扫描是否启用和服务状态，但不返回 webhook、secret、Cookie 或凭据值。

- [ ] **Step 2: 运行测试确认隐私和字段基线**

Run:

```bash
uv run pytest tests/test_provider_settings.py tests/test_schedule_web.py -q
```

Expected: PASS；如果现有接口缺少必要布尔状态，先观察正确失败再扩展薄视图。

- [ ] **Step 3: 实现默认设置摘要**

默认只显示扫描频率、最近/下次执行、模型可用性、模型名、飞书配置状态和五个服务状态。保存设置继续使用现有 CSRF 和确认机制。

- [ ] **Step 4: 实现高级维护惰性加载**

高级区使用一个显式按钮：

```tsx
<button aria-expanded={advancedOpen} onClick={() => setAdvancedOpen(value => !value)}>
  {advancedOpen ? "收起高级维护" : "展开高级维护"}
</button>
```

只有 `advancedOpen === true` 时请求在线核验、Source Markdown、数据治理和处理中心接口。折叠后停止轮询。危险操作保持原确认文案和后端门禁。

- [ ] **Step 5: 运行设置和前端回归**

Run:

```bash
uv run pytest tests/test_provider_settings.py tests/test_schedule_web.py tests/test_web.py -q
cd webui && npm run check && npm run build
```

Expected: PASS。

- [ ] **Step 6: 检查本任务差异，不提交**

Run:

```bash
git diff --check -- webui/src/views/SimpleSettingsView.tsx webui/src/views/AdvancedMaintenance.tsx webui/src/styles.css
```

Expected: 无输出。

---

### Task 7: 文档、隐私审计和部署验收

**Files:**
- Modify: `README.zh-CN.md`
- Modify: `docs/runbook-oaradar-ops.md`
- Modify: `scripts/healthcheck.sh` only if the new endpoint needs a health assertion
- Verify: all files changed by Tasks 1–6

**Interfaces:**
- Documents: 三个默认页面、业务口径、高级维护入口和故障判断
- Verifies: `/api/simple-status`、静态前端、服务与定时器

- [ ] **Step 1: 更新中文使用说明**

明确写出：

- “已完成”必须同时满足原件、MD、归类和发布；
- 待办每小时 05 分检查，夜间全量扫描每日 23:30；
- 模型兜底不等于模型成功；
- 复杂诊断位于“系统设置 → 高级维护”；
- OA 始终只读。

- [ ] **Step 2: 增加健康检查（如有必要）**

若现有 healthcheck 未覆盖新接口，只增加结构和隐私安全检查，不硬编码生产计数：

```bash
curl -fsS http://127.0.0.1:2567/api/simple-status
```

验证根键存在、headline 为中文字符串、响应不包含 `payload_json`、`structured_json`、`webhook`、`secret`。

- [ ] **Step 3: 运行完整相关测试**

Run:

```bash
uv run pytest \
  tests/test_simple_status.py \
  tests/test_console_views.py \
  tests/test_web.py \
  tests/test_schedule_web.py \
  tests/test_provider_settings.py \
  tests/test_worker.py -q
```

Expected: PASS。

- [ ] **Step 4: 运行前端生产验证**

Run:

```bash
cd webui
npm run check
npm run build
cd ..
```

Expected: TypeScript 检查和 Vite 生产构建 PASS。

- [ ] **Step 5: 运行仓库与隐私检查**

Run:

```bash
git diff --check
git status --short
rg -n "payload_json|structured_json|FEISHU_OA_SECRET|Cookie|password" \
  webui/src src/oa_knowledge/web/simple_status.py tests/test_simple_status.py
```

Expected: `git diff --check` 无输出；`rg` 结果只能是测试中的禁止断言、既有安全文案或明确的配置字段名，不得出现真实值和 OA 内容。

- [ ] **Step 6: 重启本机服务并做只读烟雾测试**

仅在前述测试全部通过后执行：

```bash
systemctl --user restart oaradar-web.service
systemctl --user is-active oaradar-web.service oaradar-worker.service oaradar-markdown-worker.service
curl -fsS http://127.0.0.1:2567/api/simple-status | jq '{overall_status,done,pending,oa_activity}'
```

Expected: 三个服务均 `active`；接口返回结构完整。不得为了验收触发 OA 扫描、在线核验、数据清理或飞书测试消息。

- [ ] **Step 7: 人工验收默认界面**

在浏览器确认：

1. 一级导航只有三个入口；
2. 首页能直接读出“已办是否全部做好”；
3. 待办卡显示频率、飞书和模型兜底；
4. 已办资料分页、搜索和六种状态筛选正常；
5. 高级维护默认关闭，展开后旧能力仍可访问；
6. OA 正文和凭据不出现在默认页面和浏览器控制台。

- [ ] **Step 8: 最终差异复核，不提交或推送**

Run:

```bash
git diff --stat
git diff --check
```

向用户报告修改文件、测试结果、当前真实业务计数和任何未解决异常。保留工作树供用户或主模型进一步检查。
