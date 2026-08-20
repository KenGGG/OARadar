# OARadar V2 运行手册

OARadar V2 是本地、只读的个人 OA 智能工作台。它只自动运行三条链路：待办通知、已办原件归档和 Markdown 交付。OA 集成不得审批、回复、删除、转发或修改任何 OA 记录；飞书凭据只从环境变量注入，绝不写入 YAML 或 Git。

## 1. V2 运行边界

```text
OA Pending → 摘要 → 飞书 → 清理临时业务内容
OA Done    → 原始下载 → 本地校验 → 不可变归档
本地归档   → ParseArtifact → Source Markdown + _index.md
```

- Pending 是短生命周期通知材料。只有飞书明确 `sent` 后才清理正文、临时附件、快照和摘要；去重与投递台账保留。`unknown_outcome` 绝不自动重发。
- Done 原件是不可变事实底稿。归档是否成功只由本地文件存在性、大小、SHA-256、相对路径和深度边界决定；Markdown 失败不会回退归档成功。
- Markdown Worker 只读取数据库和 `data_root` 下已验证的本地文件，不访问 OA、不发送飞书。新附件 Markdown 只能由有效 `ParseArtifact` 经 `source_markdown/service.py` 原子发布。
- 容器遍历最多十层。第十层仍有子级时记录 `depth_limit_reached`，事项不得显示完成。
- Curated、知识投影、Vault、Review、数据治理、Online Audit、Policy、Backfill 与高级维护不属于 V2 自动链路；旧代码和数据保留兼容期，但不新增入口或写入。

## 2. 长期运行的进程

| 进程 | systemd 单元 | 职责 |
| --- | --- | --- |
| OA Worker | `oaradar-worker.service` | 串行执行只读 Pending 与 Done Archive 任务 |
| Markdown Worker | `oaradar-markdown-worker.service` | 执行本地 Markdown Delivery 任务 |
| 每小时扫描 | `oaradar-hourly.service` / `.timer` | Pending 扫描和 Done 增量扫描，只创建持久任务 |
| 夜间扫描 | `oaradar-nightly.service` / `.timer` | Done 全量清单核对，只创建持久任务 |

定时器和 Web 均不直接启动浏览器。OA Worker 通过既有浏览器租约串行执行 OA 只读操作；Markdown Worker 使用独立的本地解析/GPU 资源。调度与 Worker 使用既有 `OperationJob`、`PipelineTask`、租约和退避机制，不增加第二套协调器。

OA Worker 使用 `PrivateTmp=false`，因为 Chrome 持久配置依赖用户 `/tmp` 中的 singleton/IPC 状态；Web 与 Markdown Worker 保持 `PrivateTmp=true`。所有服务均使用 `NoNewPrivileges=true` 和 `UMask=0077`。

## 3. 调度与任务口径

| 命令 | 作用 |
| --- | --- |
| `oa schedule bootstrap` | 首次建立 Pending 与 Done 基线；Pending baseline 不下载详情、不摘要、不发送飞书 |
| `oa schedule hourly` | 发现 Pending 新增/变化及 Done 增量 |
| `oa schedule nightly` | 核对 Done 全量清单，修复缺失的核心任务入队 |
| `oa schedule enqueue hourly\|nightly` | 创建持久调度任务，供 systemd 使用 |
| `oa schedule status` | 查看最近扫描运行记录 |

扫描均写入 `runs` 表。Pending 状态机为 `baseline/detail_sync → pending_parse → pending_summary → notify_feishu → pending_cleanup`；Done Archive 在 `archive_verify` 成功后创建独立 Markdown Delivery 任务并完成。Done 不解析、不分类、不发布 Markdown。

可恢复错误使用既有有限指数退避；进程中断后仅过期 lease 回到原 stage。每次重试先核对已有输出事实。退役 stage 在 lease 过期后标记 `RETIRED_STAGE`，不再执行或自动重试。

## 4. Web 控制台与安全

一级导航固定为：**总览 / 待办通知 / 已办归档 / Markdown 输出 / 设置**。默认页为总览；它只聚合三条 V2 流水线的业务状态和需要处理项，不以 Curated、Review 或 Online Audit 判断核心状态。

控制台继续仅监听 loopback，并保留 Host/Origin 校验、CSRF、安全响应头和可选本地认证。GET 为只读；写入动作均需通过现有认证和 CSRF 门禁。API 不返回 OA 正文、附件内容、模型输入输出、飞书正文、凭据、Cookie 或浏览器状态。

下列旧地址统一返回 JSON 404，不能落入 SPA HTML 回退：

```text
/api/audits/*
/api/governance/*
/api/reviews/*
/api/policies/*
/api/batches/*
/api/backfill/*
/api/lifecycle/knowledge*
/api/lifecycle/processing*
/api/maintenance/*
```

核心 schedule、登录、健康、待办、已办、Markdown 和设置接口保留业务语义；列表服务端分页，绝对路径只在设置页可见。

## 5. 数据去向与存储

```text
Pending 新增/变化 → 本地临时摘要 → 飞书 → 清理
Done 新增/变化    → archive/raw/oa/done/... → 已验证原件
已验证原件        → active ParseArtifact → Source Markdown + _index.md
```

归档路径和交付路径均按相对 `data_root` 的 POSIX 路径记录。`archive/raw/oa/done` 下的已验证原件不得覆盖、移动或删除。历史成功 Markdown 在文件与记录哈希一致时保留，不会因 V2 升级而重新解析。

每个已归档事项在其原始 OA 镜像目录对应的 Source Markdown 目录中有一个 `_index.md`，无附件事项也不例外。索引只包含来源元数据、四个分类字段和附件链接/状态；不得包含 AI 知识总结。分类只更新 Frontmatter，不移动原件或 Markdown 路径。OARadar 只写配置的 Source Markdown 目录，不写 `llm_wiki` wiki 或独立 Obsidian Vault。

## 6. 安装与日常检查

```bash
./scripts/install-systemd-user.sh \
  --project-root "$PWD" \
  --config "$PWD/config.yaml" \
  --timezone Asia/Shanghai
```

安装脚本渲染并启用两项 worker 服务和 hourly/nightly 两组 service/timer。机密写入 `~/.config/oaradar/env`（权限 600），不进入 YAML。

```bash
./scripts/healthcheck.sh
uv run oa schedule status --config config.yaml
uv run oa notifications status --config config.yaml
uv run oa doctor --config config.yaml
```

`healthcheck.sh` 检查 worker、timer、最近扫描、数据库、OA 登录、Markdown 积压、飞书配置和磁盘容量。失败退出码非零。飞书没有测试发送按钮；只观察自然产生的 Pending 通知及其投递账本。

## 7. 合并前本机只读冒烟与回滚

合并前冒烟必须先在仓库外备份本地配置、数据库和 systemd unit，并暂停 timer。真实数据只在本机使用，报告仅记录脱敏数量、字节数和哈希汇总，绝不提交 Git。冒烟只验证服务可启动、baseline 不发送历史通知、既有成功 Done 不重下、既有成功 Markdown 不重解析、一个本地事项可完成 `ParseArtifact → Source Markdown → _index.md`，以及 V2 页面和退役 JSON 404 可读。

回滚时停止服务，恢复代码、数据库和 unit 备份，再 `daemon-reload` 并重启服务。不得以删除 OA 原件、归档或任务账本的方式回滚。合并后在 `main` 进行 24 小时和 7 天自然运行观察；观察期不创建 OA 测试事项、不发送测试飞书。

## 8. 公开发布检查

每次公开提交前运行：

```bash
uv run python scripts/check_public_release.py
```

不得提交 `data/`、浏览器 profile、Cookie、凭据、Playwright trace、真实 HTML、下载文件、数据库、运行日志或任何真实 OA 内容。测试 fixture 必须为合成或不可逆脱敏数据。
