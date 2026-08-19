# OARadar V2 Phase 5 Lightweight Console Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (- [ ]) syntax for tracking.

**Goal:** 交付只展示待办、归档、Markdown 与设置的轻量 Web 控制台。

**Architecture:** 复用 simple_status、console_views 和现有 FastAPI 路由；只在缺少业务语义时扩展现有响应。前端基于 Simple Views 增加 Pending/Markdown 页面，不建立第二套控制台或 API facade。

**Tech Stack:** FastAPI、SQLAlchemy、pytest、React、TypeScript、Vite

**Spec:** `docs/superpowers/specs/2026-08-19-oaradar-v2-convergence-design.md`

## Global Constraints

- 总览不得读取 Curated、Review 或 Online Audit 决定核心状态。
- 清理后的 Pending 不返回业务字段。
- 归档与 Markdown 状态分开。
- 列表服务端分页，每页 50。
- API 优先复用现有路径。

---

### Task 1: 收敛业务状态与现有 API

**Files:**
- Modify: `src/oa_knowledge/web/simple_status.py`
- Modify: `src/oa_knowledge/web/console_views.py`
- Modify: `src/oa_knowledge/web/app.py`
- Test: `tests/test_simple_status.py`
- Test: `tests/test_console_views.py`
- Test: `tests/test_web.py`

**Interfaces:**
- Produces: `simple_status(settings) -> dict` with pending/archive/markdown cards
- Produces paged pending, done and item-grouped Markdown payloads through existing routes
- Preserves existing CSRF, loopback and JSON error middleware

- [ ] **Step 1: Write failing business-status tests**

Assert three independent cards; null/unavailable on missing data; no curation joins; cleaned Pending redaction; separate archive/Markdown filters; item-grouped Markdown with classification; existing routes reused; retired routes 404.

- [ ] **Step 2: Run tests and verify RED**

Run: `uv run pytest tests/test_simple_status.py tests/test_console_views.py tests/test_web.py -q`
Expected: FAIL because current done status uses Curated and Markdown output is flat.

- [ ] **Step 3: Implement response convergence**

Replace curation-derived status with archive/Markdown facts, paginate Pending and Markdown by item, enforce operation preconditions with 409, and reuse current route names. Add no facade and no duplicate route set.

- [ ] **Step 4: Run focused tests and verify GREEN**

Run: `uv run pytest tests/test_simple_status.py tests/test_console_views.py tests/test_web.py -q`
Expected: PASS; response fixtures contain no OA body, credentials or absolute list paths.

- [ ] **Step 5: Commit**

~~~bash
git add src/oa_knowledge/web/simple_status.py src/oa_knowledge/web/console_views.py src/oa_knowledge/web/app.py tests/test_simple_status.py tests/test_console_views.py tests/test_web.py
git commit -m "refactor: expose three core business statuses"
~~~

### Task 2: 五入口轻量前端

**Files:**
- Modify: `webui/src/App.tsx`
- Modify: `webui/src/types/simple-status.ts`
- Modify: `webui/src/views/SimpleOverviewView.tsx`
- Modify: `webui/src/views/SimpleDoneView.tsx`
- Modify: `webui/src/views/SimpleSettingsView.tsx`
- Create: `webui/src/views/SimplePendingView.tsx`
- Create: `webui/src/views/SimpleMarkdownView.tsx`
- Modify: `webui/src/styles.css`
- Test: `tests/test_web.py`

**Interfaces:**
- Consumes Task 1 response contracts
- Produces View union: overview | pending | done | markdown | settings

- [ ] **Step 1: Write failing bundle/navigation tests**

Assert exactly five navigation labels, three overview cards, no AdvancedMaintenance/Curated/Audit/Review/Policy/Queue/Lease text, and cleaned Pending placeholder text.

- [ ] **Step 2: Run check/tests and verify RED**

Run:
~~~bash
cd webui
npm run check
cd ..
uv run pytest tests/test_web.py -q
~~~
Expected: FAIL because Pending/Markdown views and five-entry navigation are missing.

- [ ] **Step 3: Implement the views**

Wire existing endpoints, preserve search/filter/page across 5-second refresh, split Done status columns, show item-grouped Markdown, and keep secrets as configured/unconfigured only.

- [ ] **Step 4: Build and verify GREEN**

Run:
~~~bash
cd webui
npm run check
npm run build
cd ..
uv run pytest tests/test_web.py tests/test_console_views.py -q
~~~
Expected: all commands exit 0 and production static assets match source.

- [ ] **Step 5: Commit**

~~~bash
git add webui/src/App.tsx webui/src/types/simple-status.ts webui/src/views/SimpleOverviewView.tsx webui/src/views/SimpleDoneView.tsx webui/src/views/SimpleSettingsView.tsx webui/src/views/SimplePendingView.tsx webui/src/views/SimpleMarkdownView.tsx webui/src/styles.css src/oa_knowledge/web/static tests/test_web.py
git commit -m "feat: deliver lightweight OARadar console"
~~~
