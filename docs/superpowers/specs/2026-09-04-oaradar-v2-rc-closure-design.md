# OARadar V2 RC 发布收口设计规格

**日期：** 2026-09-04  
**状态：** 待用户书面审阅  
**产品范围：** Pending Assistant、Done Archive、Markdown Delivery、轻量 Web 控制台

## 1. 目的

OARadar V2 不再进入功能扩建阶段。当前工作的唯一目标，是以当前本地工作区为事实源，修复候选发布阻塞，完成三条核心流程的自动化验证和有界本机冒烟，并给出唯一、可审计的候选发布结论。

本规格不重新设计三条流水线。业务结构、状态边界和数据语义继续以现有 [V2 架构收敛设计](2026-08-19-oaradar-v2-convergence-design.md)及 Phase 2、3、4 实施计划为准。本规格只增加发布收口职责、门禁、证据格式和停止条件。

禁止使用完成度百分比。代码开发是否完成只由 `OARADAR_V2_RC_PASS` 决定；部署后是否稳定只由 `OARADAR_V2_STABLE_PASS` 决定。

## 2. 角色与里程碑

Codex 在本阶段担任“OARadar V2 发布收口负责人”，而不是产品设计者或功能开发者。其职责是保护现有工作区、验证候选版本、对真实缺陷作最小修复、保留可复核证据，并在门禁结束时明确停止。

### 2.1 `OARADAR_V2_RC_PASS`

以下条件全部成立，即宣布“OARadar V2 三条核心流程的代码开发已经完成，进入候选发布运行观察”：

1. 完整 Python 测试从头运行至最终汇总，零失败；
2. 前端 TypeScript 检查和生产构建通过；
3. Public Release Checker 零发现；
4. 三条核心流程的硬性验收项均有通过的自动化测试；
5. 有界、只读、无测试飞书的本机冒烟通过；
6. Web 展示与后台数据库事实一致；
7. 未发现数据覆盖、重复通知、重复下载、重复解析、越权写目录或 OA 写操作等高风险问题。

24 小时和连续 7 天观察不属于此里程碑的阻塞项。

### 2.2 `OARADAR_V2_STABLE_PASS`

候选版本获授权合并、部署后，完成 24 小时和连续 7 天稳定性观察，且没有高风险缺陷，才宣布稳定发布。观察期内不得以等待时间为理由继续新增功能；一般展示问题和分类准确率问题进入后续 backlog。

## 3. 唯一事实源与既有合同

- 当前本地工作区、当前 HEAD、当前本地配置和当前本地数据是唯一事实源；`origin/main` 只用于计算差异，不用于推断本地能力。
- 必须先读取 `AGENTS.md`、README、中英文安全与运维文档、V2 convergence spec，以及 Phase 2、3、4、6 计划。
- 计划中引用的命令或测试文件必须与本地实际文件核对；不存在的路径不得作为完成证据。
- 历史计划中的未勾选框不是当前缺陷。只有本规格的门禁结果决定 RC 状态。

三条固定流程为：

```text
Pending Assistant
OA Pending → baseline/detail_sync → pending_parse → pending_summary
           → notify_feishu → pending_cleanup → completed

Done Archive
OA Done → done_discovery → done_capture_and_archive → archive_verify
        → enqueue_markdown_delivery → completed

Markdown Delivery
verified ArchivedFile → attachment_inventory → parse → source_publish
                      → classify → index_publish → completed
```

边界不可放宽：Pending 不产生永久归档和 Source Markdown；Done Archive 不解析、不分类、不发布 Markdown；Markdown Delivery 不启动浏览器、不访问 OA、不发送飞书。一条线失败不得撤销另一条线已成立的事实。

## 4. 范围冻结

在 `OARADAR_V2_RC_PASS` 前，禁止开展：

- 分类准确率、发文机关、别名、Qwen Prompt 或 Schema 优化；
- Curated、Review、Data Governance、Online Audit、Knowledge Projection、Vault Publish/Rebuild、复杂 Backfill、Advanced Maintenance；
- 新数据库、新任务表、新队列、新 Worker 框架、新协调器；
- WebUI 美化、旧代码或旧表物理清理、与三线验收无关的重构；
- 全量历史 Markdown 发布、全量回填、Adopt 或 Publication。

旧能力保持原地兼容，但不得进入 Worker、timer、默认 CLI 或日常 WebUI 核心生产链。只有阻塞本规格硬性验收的真实缺陷允许最小修改；修复不得顺带平台化或重构。

## 5. 授权与安全边界

发布收口可执行本地、可恢复、非破坏性操作：只读检查；创建本地收口分支；在仓库外备份；修改代码、测试和公开文档；执行测试、构建和迁移验证；备份后停止或启动 OARadar 自身用户服务；使用现有配置进行严格只读的小样本冒烟；创建本地原子提交。

以下操作必须另行获得用户明确授权：Git push、合并 main、推送标签、删除分支、发送测试飞书、创建测试 OA 事项、修改 OA、删除或移动真实原始附件、执行全量历史任务。

始终遵守：

- OA 集成只读，禁止审批、回复、删除、转发或改变 OA 记录；
- 真实 OA 内容、数据库、配置、日志、Cookie、浏览器状态、下载文件、HTML 和冒烟证据不得进入 Git；
- 测试只用合成或不可逆脱敏 fixture；
- 原始归档不可覆盖；路径以 `data_root` 相对路径记录；OA 标识以文本存储；
- 容器树遍历到第 10 层仍有子项时必须记录 `depth_limit_reached`，不得报告完成；
- 备份和冒烟报告保存在仓库外，最终报告只展示聚合值、相对路径和必要的短哈希前缀。

## 6. 发布收口门禁

### Gate 0：冻结和保护工作区

记录当前分支、HEAD、工作区、最近提交、本地相对 `origin/main` 的提交与 diff、worktree 状态。保留所有非本任务修改，从当前 HEAD 创建 `release/oaradar-v2-rc-closure` 本地分支，并在仓库外创建可验证的 Git bundle。不得 push、合并 main、reset、rebase、clean 或覆盖用户修改。

### Gate 1：消除已知阻塞

迁移问题必须验证而不是猜测：列出 revision 图并证明只有一个 Alembic head；核对 `0041_agnes_semantic_decision_source` 的父 revision；分别验证空白数据库升级到 head，以及合成数据库从 `0040_external_review_without_issuer` 升级到 head；确认模型与迁移一致且既有数据保留。只有证据证明测试基线过时，才最小更新断言；除非迁移图确有问题，不新增 `0042`。

公开发布问题必须删除文档中的本机绝对路径并替换为仓库相对引用或明确的合成占位符。不得修改检查器以豁免发现。

### Gate 2：完整自动化回归

先运行迁移与三条流程的专项测试，再运行完整发布检查、完整 `pytest`、前端依赖复现安装、TypeScript 检查和生产构建。完整测试不得使用 `-x`、`--maxfail=1`、skip、临时 xfail、删除测试或降低断言。

每条命令记录完整命令、exit code、passed/failed/skipped 数量和最终汇总。失败必须定位根因、作最小修复并重新运行受影响专项测试；最终必须重新运行完整门禁，不能把发现首个失败后的中间结果当作结论。

### Gate 3：有界本机真实冒烟

只有 Gate 2 全部通过后才能开始。停止 OARadar timers、OA Worker 和 Markdown Worker，按运维文档在仓库外备份数据库、配置和必要状态；记录原始附件的聚合数量、总大小和抽样 SHA256。不得删除、移动或覆盖业务数据，不发送测试飞书，不创建测试 OA 事项。

- Pending：真实 OA 只执行 baseline 并证明不打开详情、不通知、不创建待发送任务；新增、投递、清理及崩溃恢复使用合成 fixture 或 fake transport 验证。
- Done Archive：选择 1–3 个既有事项，其中至少一个有附件；两次执行并比较文件数量、大小、SHA256 和 mtime，证明不重复下载和不覆盖。
- Markdown Delivery：从已验证归档选择一个有附件事项；完成 ParseArtifact → Source Markdown → `_index.md`，两次执行证明不重解析、不重复发布且 mtime 不变；证明失败不影响 Done Archive。
- Web：核对五个核心页面、数据库聚合、退役路由、loopback 监听和仅包含核心单元的 healthcheck。

真实内容不得出现在命令记录、提交或最终报告中。

### Gate 4：候选提交与结论

修复按可独立审查和回滚的最小原子提交组织。不得提交备份、真实数据、测试输出或冒烟报告。Gate 0–3 全部通过时输出 `OARADAR_V2_RC_PASS`；只有当前环境无法自行消除的真实外部阻塞才输出 `OARADAR_V2_RC_BLOCKED`。

`RC_BLOCKED` 必须给出精确命令、错误、已完成排查、无法继续的原因，以及用户只需决定的一个具体事项。测试失败、代码缺陷或文档发现本身不是外部阻塞，必须继续修复。

## 7. 三线硬性验收合同

### 7.1 Pending Assistant

1. 首次 baseline 只建立或更新 occurrence，不打开详情、不建通知任务、不发飞书；
2. baseline 后新增或 `discovery_hash` 变化只创建一个任务；相同 hash 不重复建任务；
3. `llm.enabled=false` 时模型客户端构造与调用次数均为零；本地模型失败时规则摘要可用；
4. 同一 `input_hash` 最多一个 NotificationDelivery；
5. sent 后只进入 cleanup；崩溃恢复不得重发；cleanup 失败重试只继续 cleanup；
6. `unknown_outcome` 不得自动重发；
7. Pending 不产生 Done Archive、ParseArtifact 或 Source Markdown。

### 7.2 Done Archive

1. 新 Done 能够只读发现、下载并建立 OAItem/ArchivedFile；
2. 本地验证覆盖存在性、大小、SHA256 和允许路径；明确无附件形成 `no_attachment`；
3. 深度限制、缺失文件、大小不符和哈希不符均阻止归档完成；
4. manifest 未变化的成功事项不再打开详情或重复下载；已验证文件不覆盖；
5. `archive_verify` 成功只创建一个 Markdown Delivery 任务；
6. Markdown 失败不撤销 Done Archive 成功；
7. Done 路径不调用解析器、LLM、Curated、Online Audit 或 Knowledge 流程。

### 7.3 Markdown Delivery

1. 输入只能是本地已验证 ArchivedFile；不得启动浏览器、访问 OA 或发送飞书；
2. 新附件 Markdown 只能从 active ParseArtifact 发布，不得绕过；
3. 每个支持的附件生成 Source Markdown；每个 Done 事项只生成一个稳定 `_index.md`，无附件事项也可生成；
4. `_index.md` 包含最小分类 Frontmatter 和附件链接；分类变化不改变归档或 Markdown 路径；
5. 成功且哈希一致的输入重跑时不重解析、不重复发布且 mtime 不变；
6. 单附件失败不破坏其他附件或 Done Archive；
7. Source Markdown 不写入 llm_wiki 的 `wiki/` 目录。

每个验收项必须映射到本地实际存在的测试名称。覆盖不足时优先在既有流程或 E2E 测试文件中增加一个最小测试，不创建新测试框架。

## 8. 证据与最终报告

最终报告必须包含：当前分支与 HEAD、相对 `origin/main` 的提交、修改文件与原因、Alembic head 与两条升级路径、Public Release Checker、完整 pytest、前端检查和构建、三线验收矩阵、脱敏冒烟证据、未解决问题、数据安全风险、下一步唯一需授权操作，以及唯一状态标记。

三线验收矩阵每行包括“验收项、对应测试、自动化结果、冒烟证据”。不得用“基本完成”“大约完成”或百分比代替证据。

## 9. RC 之后

用户审阅 `OARADAR_V2_RC_PASS` 后，才可另行授权本地 main 合并、`0.2.0rc1` 版本提交和本地部署。Git push、远程标签和远程分支删除仍需独立授权。

部署后的 24 小时和连续 7 天观察只决定是否达到 `OARADAR_V2_STABLE_PASS`。只有数据丢失、原件覆盖、重复通知、OA 被修改、越权写目录或核心流程持续失败等高风险缺陷，才重新打开核心开发。
