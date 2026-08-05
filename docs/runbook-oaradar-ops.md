# OARadar 运行手册（定时同步 + 飞书通知 + 自动运行）

本手册对应 `docs/plan-0805-01.md`（统一归档路径）与 `docs/plan-0805-02.md`
（工作时段自动扫描、飞书待办通知、已办知识归档）。所有 OA 交互均为只读，
不修改、不回复、不转发任何 OA 记录；飞书 webhook/secret 只从环境变量注入，
不写入 YAML。

## 1. 长期运行的进程

| 进程 | systemd 单元 | 作用 |
| --- | --- | --- |
| OA 操作 worker | `oaradar-worker.service` | 处理实时/历史队列：抓取、附件、解析、摘要、飞书通知 |
| Markdown worker | `oaradar-markdown-worker.service` | 把已验证附件转成 Markdown 知识文档 |

推荐用 `scripts/systemd/` 下的用户服务常驻运行（安装见第 6 节，细节见
`scripts/systemd/README.md`）。三者（worker ×2 + 扫描）之间有三重去重保护：
systemd 单实例 + `flock` 文件锁 + 数据库 `ResourceLease` 租约，避免同一浏览器
会话被并发占用。

## 2. 定时同步编排

每次扫描都记录进 `runs` 表（`stage` ∈ `scheduled_bootstrap` /
`scheduled_hourly` / `scheduled_nightly`），并写入结构化 `summary_json`，因此
Web 与 CLI 都能看到“最近一次扫描时间、待办新增/变化、已办新增、夜间补齐结果”。

| 命令 | 作用 |
| --- | --- |
| `oa schedule bootstrap` | 首次全量建库：完整待办快照 + 已办清单基线 |
| `oa schedule hourly` | 工作时段每小时：完整待办快照（仅新增/变化入队并通知）+ 已办 known-boundary 增量 |
| `oa schedule nightly` | 工作日晚间：完整已办清单核对、入队所有待下载、修复漏入队的 Markdown 任务 |
| `oa schedule status` | 查看最近的定时运行记录 |

CLI 内部使用 `record_scheduled_run` / `close_scheduled_run` 记录开始与结束；
扫描遇到安全上限（超过 20 页）会记为 `partial` 而非伪造 `completed`。

调度员（`oaradar-hourly.timer` / `oaradar-nightly.timer`）在 `Asia/Shanghai`
时区运行：周一至周五每小时 `:05` 分（09–17）、每晚 `23:30`。安装脚本会打印
`systemd-analyze calendar` 计算的最终触发时间。

## 3. 飞书通知生命周期

待办通知走 `pending_summary` 类型的 `NotificationDelivery` 账本，状态机：

```
queued → sending → sent | retry_wait | unknown | failed | skipped_disabled
```

- `sent`：送达成功。
- `retry_wait`：可重试失败（连接失败 / 限流 / 服务端 5xx），由后续扫描或手动重试重发。
- `failed` / `unknown`：不可重试（业务拒绝码 10001/10005/19001/19021/20003、
  或超时等未知结果），进入“parked”等待人工处理，**不会无限重试**。
- `skipped_disabled`：飞书未启用时跳过。

控制命令：

| 命令 | 作用 |
| --- | --- |
| `oa notifications status` | 飞书可达性、最近成功/失败时间、各状态计数 |
| `oa notifications test-feishu` | 发送**合成**连通性测试卡片（不含任何真实 OA 数据）；未就绪或失败退出码非 0 |
| `oa notifications retry <delivery_id>` | 按 id 重发 parked 的 `pending_summary` 通知 |

`validate_feishu_runtime_config` 在发送前校验 webhook 路径必须以
`/open-apis/bot/v2/hook/` 开头；配置错误**不会**被当作发送成功。所有错误文本
会脱敏（URL/secret 被折叠），不进入账本明文。

## 4. Web “自动运行”面板与 API

Web 控制台新增「自动运行」视图，展示：每小时扫描是否启用、下次运行时间、
最近一次扫描时间、待办新增/变化数、已办新增数、OA 登录状态、飞书成功/失败数、
Markdown 队列积压、夜间补齐结果，并提供「每小时扫描 / 夜间补齐 / 飞书连通性测试」
触发按钮。所有操作仍受 loopback + 认证 + CSRF 保护。

对应 API：

```
GET  /api/schedule/status
POST /api/schedule/hourly        # 202，后台启动 oa schedule hourly
POST /api/schedule/nightly       # 202，后台启动 oa schedule nightly
GET  /api/notifications/status
POST /api/notifications/test     # 202 成功 / 409 未就绪或失败
POST /api/notifications/{id}/retry
```

触发端点复用与 systemd 定时器完全相同的子进程，不在 Web 进程内重复任何浏览器
逻辑；运行结果由该子进程自身写入 `runs` 表，面板随后即可读到。

## 5. 数据去向

```
待办新增/变化 → 本地摘要 → 飞书卡片（幂等账本防重复）
已办新增      → 原始归档 → 立即入 Markdown 队列 → markdown-worker → 知识库
漏入队的文件  → nightly 扫描修复并补入 Markdown 队列
```

`oa knowledge audit-handoff` 可审计“已办归档 → Markdown 知识库”链路：核对已验证
源文件、Markdown 任务、成功/待处理/失败数、孤儿导出与缺失路径。

## 6. 安装（systemd 用户服务）

```bash
./scripts/install-systemd-user.sh \
  --project-root "$PWD" \
  --config "$PWD/config.yaml" \
  --timezone Asia/Shanghai
```

脚本自动检测项目路径、uv、Python 环境、config、data_root、systemd 用户目录与
环境变量文件，渲染 6 个 unit 到 `~/.config/systemd/user/` 并 `daemon-reload`、
启用、启动。敏感值写入 `~/.config/oaradar/env`（chmod 600），不进 YAML。

卸载：

```bash
./scripts/uninstall-systemd-user.sh
```

## 7. 健康检查

`scripts/healthcheck.sh` 覆盖以下检查（对应 plan-0805-02 §5.4）：

- 数据库可用、schema 完整；
- 两个常驻 worker 是否在运行；
- 定时器是否启用、下次触发时间；
- 飞书配置是否 `ready`；
- 最近一次扫描是否在预期窗口内、是否存在 `failed`/`unknown` 通知堆积。

退出码非 0 表示有不健康项，适合接入监控或 cron 报警。

## 8. 统一归档路径（一次性迁移）

代码现已将新数据写入统一路径：

```
data/archive/raw/oa/done/<YYYY>/<MM>/<标题>_<事项ID>/
data/archive/raw/oa/pending/<logical_item_id>/<snapshot_id>/
```

历史上写入的 `data/raw/done`、`data/raw/pending` 旧数据需要通过迁移命令统一。
**迁移会移动磁盘目录并改写数据库中的相对路径，请务必先停 worker 并备份。**

### 步骤

1. 停止 worker（systemd 或进程）：
   ```bash
   systemctl --user stop oaradar-worker.service oaradar-markdown-worker.service
   ```
2. 备份数据库：
   ```bash
   cp data/state/oa.db data/state/oa.db.bak-$(date +%Y%m%dT%H%M%S)
   ```
3. 预检（不动任何东西）：
   ```bash
   uv run oa archive migrate-paths --dry-run --config config.yaml
   ```
   输出 `{"total":..., "migrated":..., "already_correct":..., "skipped":..., "failed":0}`。
4. 执行：
   ```bash
   uv run oa archive migrate-paths --yes --config config.yaml
   ```
5. 完整审计，确认无遗漏/冲突：
   ```bash
   uv run oa audit --config config.yaml
   ```
6. 恢复 worker：
   ```bash
   systemctl --user start oaradar-worker.service oaradar-markdown-worker.service
   ```

### 说明

- 迁移是逐项、幂等的：已位于 `archive/raw/oa` 前缀的项会被跳过；目录冲突
  （目标已存在）会跳过该项并记录到 `failed`，不会中断整体。
- Markdown 工作区文件（`workspace/raw/sources/oa/...`）在迁移中**不移动**：
  其相对路径只含 `done/...` 或 `pending/...` 后缀，与归档原始前缀无关。
- 迁移命令只改写 `files.local_relpath`、`oa_items.archive_relpath`、
  `oa_manifest.archive_relpath`、`batch_items.archive_manifest_relpath`。

## 9. 回滚

- **应用回滚**：`git checkout <上一稳定提交>` 后重新 `uv sync` 与
  `npm run build`（前端），并 `systemctl --user daemon-reload` 重启服务。
  数据库 schema 由 Alembic 管理，回滚应用前请确认目标提交对应的迁移版本，
  必要时用第 8 节备份的 `oa.db.bak-*` 恢复数据库。
- **定时任务回滚**：`scripts/uninstall-systemd-user.sh` 停止并禁用全部单元，
  恢复为纯手动 `oa schedule ...` 运行。
- **飞书通知回滚**：在 provider 设置中将飞书「启用」关闭，新通知会记为
  `skipped_disabled`，已发送记录保留在账本中可供审计，不会重发。
- 所有回滚操作均为本地、可逆，不触碰任何 OA 线上数据。
