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
`scripts/systemd/README.md`）。定时器和 WebUI 只创建持久 `OperationJob`，不直接
启动浏览器；唯一的 OA Worker 串行执行所有 OA 只读操作。systemd 单实例、`flock`、
持久任务和数据库租约共同避免同一浏览器会话被并发占用。

OA Worker 明确使用 `PrivateTmp=false`：Chrome 持久配置依赖用户 `/tmp` 中的
singleton/IPC 状态，私有临时目录会使 OA 页面返回 `net::ERR_ACCESS_DENIED`。
该例外仅适用于需要 Chrome 的 OA Worker；Web 与 Markdown Worker 仍使用
`PrivateTmp=true`，所有服务继续保留 `NoNewPrivileges=true` 和 `UMask=0077`。

## 2. 定时同步编排

每次扫描都记录进 `runs` 表（`stage` ∈ `scheduled_bootstrap` /
`scheduled_hourly` / `scheduled_nightly`），并写入结构化 `summary_json`，因此
Web 与 CLI 都能看到“最近一次扫描时间、待办新增/变化、已办新增、夜间补齐结果”。

| 命令 | 作用 |
| --- | --- |
| `oa schedule bootstrap` | 首次全量建库：完整待办快照 + 已办清单基线 |
| `oa schedule hourly` | 工作时段每小时：完整待办快照（仅新增/变化入队并通知）+ 已办 known-boundary 增量 |
| `oa schedule nightly` | 工作日晚间：完整已办清单核对、入队所有待下载、修复漏入队的 Markdown 任务 |
| `oa schedule enqueue hourly\|nightly` | 创建持久调度任务；供 systemd 和 WebUI 使用，不直接打开浏览器 |
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

Web 控制台默认一级导航为「总览 / 已办资料 / 系统设置」。「总览」通过
`GET /api/simple-status` 聚合两条业务链路的口语化结论（已办知识库是否完成、
待办飞书是否正常、OA 后台状态），并集中列出需人工处理的问题；「已办资料」按
单一简化状态（等待下载 / 等待 MD 化 / 等待归类 / 已完成 / 需要处理 / 已按规则排除）
分页展示，支持搜索与状态筛选。「系统设置 → 高级维护」承载原「自动运行」视图、
在线逐项核验、Source Markdown 明细与复核、数据治理、处理中心等复杂能力，默认
折叠，首次展开才加载。新增只读状态接口：

```
GET  /api/simple-status            # 极简业务状态聚合（脱敏，不含 OA 正文/凭据）
GET  /api/done-archives?simple_status=attention   # 按简化状态服务端筛选分页
```

原有「自动运行」面板能力仍在「高级维护」中可用，展示：每小时扫描是否启用、下次运行时间、
最近一次扫描时间、待办新增/变化数、已办新增数、OA 登录状态、飞书成功/失败数、
Markdown 队列积压、夜间补齐结果，并提供「每小时扫描 / 夜间补齐 / 飞书连通性测试」
触发按钮。所有操作仍受 loopback + 认证 + CSRF 保护。

对应 API：

```
GET  /api/schedule/status
POST /api/schedule/hourly        # 202，创建持久 scheduled_hourly 任务
POST /api/schedule/nightly       # 202，创建持久 scheduled_nightly 任务
GET  /api/notifications/status
POST /api/notifications/test     # 202 成功 / 409 未就绪或失败
POST /api/notifications/{id}/retry
```

触发端点与 systemd 定时器一样只入持久队列，不在 Web 进程内运行浏览器。
OA Worker 领取任务后写入运行记录，面板可持续读取进度和最终结果。

## 5. 数据去向

```
待办新增/变化 → 本地摘要 → 飞书卡片（幂等账本防重复）
已办新增      → 原始归档 → 立即入 Markdown 队列 → markdown-worker → 知识库
漏入队的文件  → nightly 扫描修复并补入 Markdown 队列
```

生产新下载统一写入 `data/archive/raw/oa/done` 与 `data/archive/raw/oa/pending`。
历史 `data/raw/done` 在逐项线上核验、哈希检查和数据库相对路径迁移完成前继续作为受保护原件
冻结保留；切换新写入根不代表可以移动、覆盖或删除这些旧原件。

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

## 8. 统一归档路径（核验后的持久迁移）

代码现已将新数据写入统一路径：

```
data/archive/raw/oa/done/<YYYY>/<MM>/<标题>_<事项ID>/
data/archive/raw/oa/pending/<logical_item_id>/<snapshot_id>/
```

历史上写入的 `data/raw/done` 由常驻 Worker 在最新一次全量线上核验完成后自动收敛。
系统只选择核验状态为“完全一致”或“本地合理保留历史资料”、且没有触发深度上限的事项；
内容变化、清单变化、访问失败和深度异常保持原位并进入复核。迁移前后会计算整个目录树的
字节指纹，拒绝符号链接、跨文件系统移动和目标冲突。迁移只做同文件系统原子改名及数据库
引用修复，不原地改写历史清单或原件内容。

如果核验发现缺件，审计派生的只读补下载任务会阻止迁移启动；补下载成功后，对应事项与
已结束的审计会自动重新排队，以新的本地原件和在线证据再次比对。只有补下载队列清空且
复核后的全量审计重新完成，路径迁移才可继续。

长时间审计期间，小时刷新新发现的已办会自动加入当前审计并增加总数；已核验事项如被小时
刷新重新归档，旧证据会失效并重新排队。即使变化发生在审计刚结束后，迁移前的覆盖检查也
会重开审计。实时待办/已办队列优先于迁移；迁移因此暂停时会在审计再次完成后自动续跑。

迁移还要求存在一次不早于本次审计开始时间的全页清单对账：扫描页数必须等于 OA 报告的
总页数，线上总数、本地清单数和审计总数必须完全相等。小时头部增量只能及时发现近期变化，
不能替代该全页证据。门禁不满足时即使逐项审计显示 100%，迁移仍不得创建。

夜间全页刷新同时为每条已办保存稳定的列表版本签名。历史无签名记录只建立一次基线，不据此
盲目重下原件；新事项、已有非空签名发生变化以及明确下载失败的事项才进入幂等实时归档队列。
这样小时扫描可以在稳定签名边界停止，夜间扫描负责发现跨越头部窗口的变化。

迁移任务会先暂停旧 Markdown 队列，并等待活动解析和生产流水线退出；每批最多 25 项，
每项独立提交，Worker 重启后继续。迁移完成后恢复原先的 Markdown 暂停状态。历史知识重建
只能领取已经位于 `archive/raw/oa/done`、且最新核验证据安全的事项，因此不会抢跑读取尚未
迁移或待复核的原件。迁移任务收敛后，缺少安全证据、核验有差异或迁移失败的历史任务会以
`ONLINE_AUDIT_REVIEW_REQUIRED` 停放为不可自动重试的人工复核项；再次点击“启动重建”也不会
绕过门禁。事项经处理后，后续全量核验与安全迁移重新证明其安全时，迁移收敛阶段会自动解除
该事项的复核门禁，并从附件清点阶段重新开始。上述状态变更只作用于任务台账，不修改原始
已办文件。
这里的“最新”指数据库中编号最新的核验运行本身必须已经完成；只要更新的核验处于排队、
运行、暂停或被重新打开状态，任何更早的已完成证据都会立即失效，历史任务不得领取。
“数据治理”页面显示聚合进度、成功数、失败数和保持原位待复核数，不显示 OA 标识或路径。

以下命令仅作为自动任务无法运行时的人工恢复手段。**人工执行会移动磁盘目录并改写数据库
中的相对路径，必须先停 worker 并备份。**

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
## Curated 知识编目

先启用 `llm.enabled` 与 `curation.enabled`，确认 `oa doctor` 中 `local_qwen` 已探测到
`qwen3.5:9b`，然后只做只读计划：

```bash
uv run oa curate plan --config config.yaml --limit 10
```

经人工确认样本范围后，运行小批次并校验：

```bash
uv run oa curate run --config config.yaml --limit 10
uv run oa curate validate --config config.yaml
uv run oa curate report --config config.yaml
```

不要直接全量回填。`needs_review` 表示元数据、来源边界或置信度不足，系统不会猜测。
Curated 目录可删除重建，Archive 和 Source Markdown 不得移动或改写。
Curated 编目会确定性去掉 Source Markdown 的来源提示和转换说明，只把“文档内容”交给
本地 qwen3.5:9b 和发布器；若降噪规则版本变化，持久任务会按新签名重建派生目录。

线上逐项核验使用数据库迁移 `0033_online_attachment_evidence` 保存逐附件角色、标识、大小和
SHA-256 证据。WebUI 的“存量知识”展示核验覆盖率和差异原因汇总；只有
`missing_download` 会自动进入只读补下载，其他差异均保留原件并等待人工复核。
若线上当前证据是本地已验证证据的严格子集，说明本地还保留了旧版本或历史抓取；系统将其
标记为 `historical_retained`，不误报为线上内容变化，也不删除历史原件。只有线上当前证据
无法在本地证据中逐字节找到时，才进入真正的清单或内容差异分类。
核验每批最多处理 25 项或运行 60 秒，批次之间主动释放任务租约；只要存在已经到期的
实时待办或实时已办任务，Worker 就暂停领取下一批核验。这样 8,000 余项历史核验可以
断点续跑，同时不会阻塞工作时段的新待办摘要和飞书通知。
核验批次主动让出执行权时，只允许实时待办和实时已办流水线插队；历史 Source Markdown
重建和 Qwen 编目必须等线上核验结束后再启动，避免一次长模型推理拖延下一批线上比对。
每个线上核验事项使用独立的 `online_audit` 超时（默认单事项 120 秒、单次下载 30 秒），
不得继承常规归档最长 1500 秒的附件总超时。这样即使单项较慢，定时扫描的最坏等待也
受单事项上限约束；附件、CAP4 批量和关联容器循环都在每个边界检查总截止时间。
超时后的部分证据不得参与一致性判断。首轮结束后访问失败项自动统一重试一次，第二次仍
失败则保留为人工复核，避免无限循环；定时扫描完成后继续同一核验断点。
历史知识任务即使已经位于 canonical 路径，也必须同时出现在最新完成审计的安全集合中：
只允许 `matched/exact_match` 或 `historical_retained/historical_retained`，并且不能触发深度上限。
内容差异、缺件、访问失败和深度异常保持排队/复核，不得发布 Source Markdown 或 Curated。
附件解析和本地模型等单项长任务使用 45 分钟滚动租约，并每 60 秒刷新一次；进程异常
退出后，新 Worker 会识别旧 owner 已失效并从持久队列安全重试，避免并发重复处理。
同优先级的实时事项按最近更新时间轮转：一个事项完成一次下载或单个附件解析后，会把
执行机会让给尚未启动的同批事项，防止大附件事项长期阻塞其他待办。
实时已办内部还遵循“先保护原件、后做派生”的阶段顺序：只要存在待下载事项，先执行
`done_capture_and_archive`，再进行附件清点、解析、Source Markdown 发布和本地编目。
因此耗时的 Qwen 编目不会挡住尚未归档的线上原件。
解析质量不足的产物会标记为 `rejected` 并进入后续复核，但只要该作业自己的派生文件仍
存在且哈希一致，就视为已经解析，不能因其未成为 active 产物而反复入队。只有派生文件
缺失或哈希不一致时，才从受保护的原件重新生成；这一区分可避免低质量附件形成无限重试。
如果源文件格式暂不支持、解析作业已确认跳过、解析质量未达标或源路径不符合安全规则，
整项 Source Markdown 发布会在写出任何文件之前停止，并以不含正文的原因码进入“存量知识 →
Source Markdown 人工复核”。这类事项不会被错误报告为完成，也不会在附件清点和发布阶段之间
无限循环；人工补充解析能力或处理异常后，再从对应已办事项重试。
复核列表中的“重新检测并继续整理”只会把该事项的失败知识任务退回附件清点阶段，
不会批量重试其他失败项，也不会写回 OA；若问题仍未解决，事项会再次安全进入复核列表。
系统设置中的“重试失败任务”只重置标记为可恢复的失败；不支持格式、解析质量不足等
非可恢复失败保持停放，必须从对应人工复核记录执行范围明确的重试。
生产编目对 qwen3.5:9b 使用 12,000 token 的单次输入预算；更长来源先分块提炼再归并，
不能因为模型声明了更大的上下文窗口就把整份长文一次性塞入模型。对本地读取超时按
`llm.max_retries` 有限重试；生产本地推理超时为 600 秒，任务与 GPU 资源使用更长的滚动
租约覆盖该窗口。超过次数后交还持久任务退避，避免静默丢项或无限重试。
编目提示只向模型暴露 `S1`、`S2` 等短来源别名；模型返回后再由确定性映射恢复数据库来源键，
避免长键被模型改写。模型漏填正式文件的机构或文号时，只允许从它所引用的 Source Markdown
原文中按高精度规则补齐，找不到证据仍进入人工复核。最终 JSON 不符合 Schema 时直接记录
`schema_invalid` 待复核，不进行无意义的网络式重试。
规则、提示或 Schema 版本变化时，Worker 会为旧版最终结果创建独立、幂等的历史重编目任务；
不篡改已完成的实时任务账本，也不会抢占正在进行的线上核验。输入签名与版本均未变化时继续
自动跳过。

## 待办定时摘要

`oaradar-hourly.timer` 只创建持久调度任务；OA Worker 完成只读待办刷新后，按优先级依次完成
详情、附件解析、本地摘要和飞书发送。
同一内容版本使用稳定幂等键，不会重复发送；`unknown_outcome` 必须人工核对，普通重试接口会
拒绝再次发送，WebUI 只显示“发送结果待确认”告警，不显示重发或清理按钮。所有清理路径
（包括历史兼容的 `force` 参数）都必须先证明飞书台账状态为 `sent`，不能绕过投递确认。
只有飞书明确返回成功后才执行待办临时正文清理。可用以下命令查看状态：

```bash
uv run oa schedule status --config config.yaml
uv run oa notifications status --config config.yaml
uv run oa doctor --config config.yaml
```

## 数据治理与容量视图

WebUI“数据治理”页按四级生命周期展示本地数据：永久原件与账本、活动中间产物、
可重建投影、临时/缓存/过期备份。页面同时汇总受保护原始已办、已核验原件、活动任务、
待人工复核、数据库大小、磁盘余量和隔离区容量。浏览器只会收到类别、数量、字节数与
数据库引用计数，不会收到候选文件名、路径、OA 标题或正文。

“最近预检的可治理资料”取自最近一次清理计划，分别显示浏览器缓存、运行报告、过期备份、
已发送待办残留和可重建派生资料。这里的数字只是候选汇总，不表示文件已经删除；隔离执行前
仍会再次验证路径边界、活动任务、数据库引用、唯一原件保护和待复核冻结规则。隔离区默认
可恢复，第一轮清理不得永久清除；只有用户另行确认保留期并输入精确确认串后，才允许创建
永久清除任务。
