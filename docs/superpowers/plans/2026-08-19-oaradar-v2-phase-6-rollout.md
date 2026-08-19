# OARadar V2 Phase 6 Rollout Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (- [ ]) syntax for tracking.

**Goal:** 完成产品文档、自动化验证、本机只读冒烟、main 合并与发布稳定观察。

**Architecture:** 开发阶段结束后先在实施分支完成全量自动化验证和有界本机冒烟，随后请求授权并合并 main。24 小时和 7 天观察只在 main 上执行，不回阻前序开发阶段。

**Tech Stack:** pytest、Vite、public release checker、systemd user units、Git

**Spec:** `docs/superpowers/specs/2026-08-19-oaradar-v2-convergence-design.md`

## Global Constraints

- 不发送测试飞书、不创建 OA 测试事项、不修改 OA。
- 本机备份、冒烟记录和观察报告不得进入 Git。
- 合并、推送标签和删除远程分支均需用户明确授权。
- 回滚不得删除业务数据。

---

### Task 1: 文档、全量验证与本机冒烟

**Files:**
- Modify: `README.md`
- Modify: `README.zh-CN.md`
- Modify: `docs/runbook-oaradar-ops.md`
- Modify: `docs/security.md`
- Modify: `config.example.yaml`
- Modify: `scripts/systemd/README.md`
- Modify: `scripts/healthcheck.sh`
- Test: `tests/test_public_release.py`
- Test: `tests/test_e2e_autorun.py`
- Test: `tests/test_repro_schedule_json.py`

**Interfaces:**
- Produces documented V2 operator contract and rollback checklist
- Preserves existing deploy-local and systemd service names

- [ ] **Step 1: Write failing documentation/health assertions**

Update tests to require V2 product language, three pipelines, no Curated startup instruction, no test-send instruction, and health output for OA Worker/Markdown Worker/hourly/nightly only.

- [ ] **Step 2: Run tests and verify RED**

Run: `uv run pytest tests/test_public_release.py tests/test_e2e_autorun.py tests/test_repro_schedule_json.py -q`
Expected: FAIL because docs and health text still describe knowledge/maintenance workflows.

- [ ] **Step 3: Update documentation and operational checks**

Document baseline, Pending cleanup, archive/Markdown separation, Source Markdown boundary, no new legacy platform, backup/restore and main observation. Keep example domains synthetic and credentials environment-only.

- [ ] **Step 4: Run full automated verification**

Run:
~~~bash
uv run python scripts/check_public_release.py
uv run pytest
cd webui
npm ci
npm run check
npm run build
cd ..
uv run pytest tests/test_web.py tests/test_public_release.py -q
~~~
Expected: every command exits 0 with no failed tests or public-release finding.

- [ ] **Step 5: Perform bounded local smoke and commit docs**

Before smoke, stop timers and create out-of-repo backups. Verify service startup, Pending baseline without delivery, no repeat download/parse for existing success, one local ParseArtifact→Markdown→_index path, core pages, retired JSON 404 and restore procedure. Record only pass/fail locally.

~~~bash
git add README.md README.zh-CN.md docs/runbook-oaradar-ops.md docs/security.md config.example.yaml scripts/systemd/README.md scripts/healthcheck.sh tests/test_public_release.py tests/test_e2e_autorun.py tests/test_repro_schedule_json.py
git commit -m "docs: finalize OARadar V2 operations"
~~~

### Task 2: 合并 main 与发布稳定门禁

**Files:**
- No repository file changes required unless a verified rollout defect needs its own TDD commit.
- Local-only: database/config/unit backups and redacted observation checklist.

**Interfaces:**
- Consumes all preceding phase commits
- Produces main with V2 convergence and post-merge stability decision

- [ ] **Step 1: Reconcile branch and candidate set**

Run:
~~~bash
git status --short
git log --oneline main..agent/oaradar-business-workflows
git diff --stat main...agent/oaradar-business-workflows
uv run python scripts/check_public_release.py
~~~
Expected: clean tree, reviewed commit list, no confidential candidate.

- [ ] **Step 2: Request merge authorization**

Present automated verification and local smoke evidence to the user. Do not merge, push tags or delete branches until the user explicitly approves.

- [ ] **Step 3: Merge using current graph**

If main still points to 4a0eb40, fast-forward main to the implementation branch. If main advanced, inspect its commits and use a normal merge; do not blindly rebase, reset or overwrite.

~~~bash
git switch main
git merge --ff-only agent/oaradar-business-workflows
~~~

If and only if fast-forward is impossible after inspection:

~~~bash
git merge --no-ff agent/oaradar-business-workflows
~~~

- [ ] **Step 4: Verify main and begin observation**

Run the full commands from Task 1 Step 4 on main. Deploy main using the documented backup order, then begin the local-only 24-hour checklist. At 24 hours verify no non-core writes, duplicate notification, content leak, overwritten original or unknown backlog growth.

- [ ] **Step 5: Complete 7-day stability gate**

Continue on main until seven consecutive days meet the spec. Accumulate 100 redacted Done integrity checks and 100 Markdown/index checks. On a high-risk defect, stop rollout and deploy the pre-merge commit without deleting V2 data. After success, report stable status; request separate authorization before pushing tags or deleting the remote agent branch.
