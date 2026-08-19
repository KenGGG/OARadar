# OARadar V2 Implementation Plan Index

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (- [ ]) syntax for tracking.

**Goal:** 按依赖顺序把 OARadar 收敛为三条稳定自动化流水线和一个轻量控制台。

**Architecture:** 全部改动基于现有数据库、PipelineTask、MarkdownTask、ParseJob、Worker 和业务接口；不新增数据库、任务表、协调器或兼容平台。六个阶段各自产生可测试增量，自动化测试和本机冒烟通过后合并 main，24 小时与 7 天观察在 main 上执行。

**Tech Stack:** Python 3.12、SQLAlchemy 2、Alembic、FastAPI、Typer、pytest、React、TypeScript、Vite、systemd user units

**Spec:** `docs/superpowers/specs/2026-08-19-oaradar-v2-convergence-design.md`

## Global Constraints

- OA 严格只读；不得审批、回复、删除、转发或修改 OA 记录。
- 不发送测试飞书；真实通知只能来自自然 Pending 和正常生产调度。
- 只使用合成或不可逆脱敏 fixture。
- 不提交 data、Cookie、凭据、真实快照、下载文件、数据库或日志。
- 归档路径相对 data_root；OA 标识按文本保存。
- 允许遍历到第 10 层；第 10 层仍有子级时记录 depth_limit_reached 且不得完成。
- 不新增任务表、协调器、API facade、oa legacy 平台或第三套 Queue/Worker。
- 不重下已验证原件，不重解析哈希有效的成功 Markdown。
- 每个任务使用 TDD，并在专项测试通过后独立提交。

---

## Execution Order

1. `2026-08-19-oaradar-v2-phase-1-retirement.md`
2. `2026-08-19-oaradar-v2-phase-2-pending.md`
3. `2026-08-19-oaradar-v2-phase-3-archive-markdown-boundary.md`
4. `2026-08-19-oaradar-v2-phase-4-markdown-delivery.md`
5. `2026-08-19-oaradar-v2-phase-5-console.md`
6. `2026-08-19-oaradar-v2-phase-6-rollout.md`

后续阶段只能消费前序阶段明确列出的接口。24 小时和 7 天观察不是开发阶段依赖，均在合并 main 后执行。
