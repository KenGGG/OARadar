# OARadar V2 架构收敛设计规格

**日期：** 2026-08-19
**状态：** 已确认，等待实施计划
**实施基线：** agent/oaradar-business-workflows@23f652a
**产品名称：** OARadar V2 — Personal OA Intelligence Workspace

## 1. 背景与目标

当前实施分支已经具备只读 OA 采集、原始归档、Markdown 转换、待办飞书通知、已办归档、Web 控制台、Worker、Queue 和 systemd 自动运行，也新增了 Curated、数据治理、在线核验、Review、Policy 与复杂 Backfill 等平台能力。

V2 不再扩建 OA 知识平台，而是将产品收敛为长期服务单个用户的个人 OA 智能工作台：

~~~text
OA
├─ 待办通知：发现 → 摘要 → 飞书 → 清理
├─ 已办归档：发现 → 下载 → 校验 → 永久保存
└─ Markdown 交付：解析 → Source Markdown → 分类 Frontmatter
                                               ↓
                                            Obsidian
~~~

V2 只保留四个产品组成：待办通知、已办归档、Markdown 交付和轻量控制台。首个 V2 版本的目标是停止非核心链路、修通三条核心流水线、精简 WebUI，并将成果从实施分支合并回 main。数据库瘦身、旧代码物理删除和插件化属于后续独立工作。

## 2. 强制安全边界

- 所有 OA 集成严格只读。不得审批、回复、删除、转发或修改 OA 记录。
- Pending 数据是短生命周期通知材料，不是永久知识资产。
- Done 原件是不可变事实底稿；已验证文件不得被覆盖或删除。
- OA 标识按文本保存。
- 归档和产物路径按相对 data_root 的 POSIX 路径保存。
- 容器树允许遍历到第 10 层；若第 10 层仍存在需要遍历的子级，必须记录 depth_limit_reached，事项不得显示完成。
- OARadar 只写配置的 Source Markdown 目录，不写 llm_wiki 的 wiki 目录，也不维护独立 Obsidian Vault。
- 测试仅使用合成或不可逆脱敏 fixture。
- data/、浏览器 profile、Cookie、凭据、Playwright trace、真实 HTML、下载文件、数据库、运行日志及真实 OA 内容不得进入 Git。
- Web 控制台继续限制在 loopback，并保留 Host/Origin 校验、CSRF、安全响应头和可选本地认证。

## 3. 实施基线与分支策略

唯一实施基线为：

~~~text
agent/oaradar-business-workflows@23f652a
~~~

实施路线固定为：

~~~text
main@4a0eb40
  → agent@23f652a
  → V2 主线收敛提交
  → 完整验证
  → 合并或快进回 main
~~~

不得在 main 上并行开发第二套 V2，也不得在两个分支间双向合并。实施开始前建立本地冻结标签：

~~~text
legacy-main-4a0eb40
legacy-business-workflows-23f652a
~~~

标签推送、合并 main 和删除远程分支均须单独获得用户授权。继续保留 src/oa_knowledge 包名；首个 V2 版本不迁移全仓导入路径、CLI entry point、Alembic 或 systemd 名称。

## 4. 总体架构与数据原则

### 4.1 沿用现有基础设施

V2 沿用当前数据库及 Alembic 历史、当前原始归档和 Source Markdown 目录、当前 PipelineTask/PipelineEvent/OperationJob/MarkdownTask/资源租约/Worker，以及当前 systemd hourly、nightly、OA Worker 和 Markdown Worker。

不得新建 V2 数据库、全量迁移器、迁移账本、双数据库切换、新 Pending 模型或新通用任务框架。不得迁移原始归档、重建全部历史状态、重新下载或重新解析已经成功的文件。

### 4.2 三层原则

三层只定义职责，不引入新的通用事实平台：

~~~text
Evidence
  OAItem / ArchivedFile / ContentObject / ParseArtifact

Organization
  OAItem 上的最小分类字段
  _index.md Frontmatter

Consumption
  MarkdownExport 交付台账
  Source Markdown 文件
  Obsidian / llm_wiki 消费
~~~

- ParseArtifact 是新产生解析内容的唯一事实源。
- MarkdownExport 及其文件是现有的人类交付台账和交付物。
- 分类不得修改原件或解析正文。
- 消费层可以重建，不得反向修改证据层。

### 4.3 三条流水线边界

~~~text
Pending Assistant
  输入：OA Pending
  输出：NotificationDelivery + 清理后的最小台账

Done Archive
  输入：OA Done
  输出：OAItem + 已验证 ArchivedFile

Markdown Delivery
  输入：本地已验证 ArchivedFile
  输出：ParseArtifact + Source Markdown + _index.md
~~~

Pending 不产生永久归档或 Markdown；Done Archive 不解析、不分类、不发布 Markdown，只在成功后入队 Markdown Delivery；Markdown Delivery 不启动浏览器、不访问 OA、不发送飞书。一条流水线失败不得改写另一条已经成立的事实。

## 5. 非核心能力退役

以下能力退出 V2 核心生产链：

- Curated；
- Knowledge Projection、Knowledge Trial、Done Knowledge；
- Vault Publish、Vault Rebuild；
- Review；
- Data Governance；
- Online Audit；
- Policy 管理；
- 复杂 Backfill Campaign；
- Advanced Maintenance。

首个 V2 版本中的“退出”表示：

- 不由 Worker 注册或执行；
- 不由 systemd、定时任务或默认 CLI 触发；
- 不注册日常 Web API；
- 不在 WebUI 导航或设置页出现；
- 不再产生新任务或业务写入；
- 旧代码和旧表保留一个兼容周期；
- 历史读取仅通过显式只读 oa legacy CLI。

存量 queued/running 退役任务在 lease 过期后标记为非恢复失败，错误码为 RETIRED_STAGE，保留历史记录但不再执行。未来类似能力只能作为 Markdown 交付之后的可选消费者，不能成为核心流水线必经 stage。

## 6. 现有数据模型的最小调整

### 6.1 数据库策略

沿用现有数据库，只做支撑核心主线所需的增量字段和索引变更。明确不做新数据库、全量 Schema 重建、V1→V2 全量迁移、旧表删除、旧字段历史语义重写、分类历史/Review/taxonomy 平台，以及 Pending revision、HMAC 版本或新通知账本体系。

### 6.2 Pending 模型

继续使用 ItemOccurrence、ItemSnapshot、SummaryVersion、NotificationDelivery 和 PipelineTask，只修复幂等、失败恢复和清理边界。

飞书确认成功后，使用现有 pending_cleanup.py 删除正文、临时附件、快照、摘要和证据，只保留去重及投递台账。不得为清理后的记录重新持久化标题、发起人、摘要或附件名。

### 6.3 最小分类字段

若 OAItem 没有等价字段，增加：

~~~text
source_type
internal_category
external_issuer
classification_version
~~~

允许值：

~~~text
source_type:
  internal | external | unknown

internal_category:
  公司治理 | 经营管理 | 业务项目 | 风险管理 |
  财务资金 | 人力行政 | 信息化 | 其他内部 | null

external_issuer:
  规范化机构名称 | 其他外部机构 | null

classification_version:
  v1
~~~

约束：

- internal 必须填写 internal_category，external_issuer 为空；
- external 必须填写 external_issuer，internal_category 为空；
- unknown 两者均为空；
- 当前分类可覆盖更新，不保留历史；
- 分类变化不得改变归档或 Markdown 路径；
- 集团公司的内部/外部归属由配置固定，模型不得临时决定。

### 6.4 MarkdownExport 最小调整

MarkdownExport 继续作为交付台账，不另建 MarkdownDocument 表。为支持事项索引，只增加：

~~~text
oa_item_id nullable
document_kind: attachment | item_index
~~~

约束：

- 每个 (oa_item_id, schema_version) 至多一个 item_index；
- attachment 继续关联 source_file_id；
- item_index 关联 oa_item_id，source_file_id 为空；
- markdown_relpath 全局唯一。

历史 MarkdownExport.status=success 且文件与记录哈希一致时直接保留，不重新解析。历史成功记录即使 parse_artifact_id 为空，也不伪造 artifact；只有源文件变化或用户明确重建时才进入统一新链路。

## 7. Pending Assistant 规格

### 7.1 状态机与幂等

~~~text
baseline
  或
detail_sync
→ pending_parse
→ pending_summary
→ notify_feishu
→ pending_cleanup
→ completed
~~~

首次启用 V2 或显式重建 baseline 时，仅扫描并更新当前 ItemOccurrence，不下载详情、生成摘要或发送飞书。使用现有 Run 记录 baseline 完成；只有之后新增或 discovery_hash 变化的事项才能入队。

~~~text
事项任务：pending:{occurrence_key}:{discovery_hash}:v2
飞书投递：feishu:pending:{logical_item_id}:{summary_input_hash}
~~~

相同内容版本只能存在一个任务和一个投递记录。已清理事项以相同 discovery_hash 再次被扫描时，不恢复业务内容、不重新发送。

### 7.2 Stage 行为

detail_sync：只读打开 OA 详情，临时保存正文和附件，持有 oa_browser 独占租约；身份不完整或不匹配时停止。

pending_parse：仅解析本次 Pending 临时附件，产物只服务摘要，不进入 Source Markdown、Done 归档或 Curated。

pending_summary：

- llm.enabled=false 时不得初始化或调用模型；
- 模型可用时生成现有结构化摘要；
- 模型禁用、不可达、超时或输出无效时立即使用规则摘要；
- 规则摘要固定包含标题、发起人、当前节点和截止时间；
- 规则摘要是正常兜底，不阻止飞书；
- input_hash 不变时不得创建重复摘要版本。

notify_feishu：

- 发送前查询既有 NotificationDelivery；
- 已为 sent 时不再发送，直接进入清理；
- 明确连接失败、服务端失败或限流可以退避重试；
- unknown_outcome 禁止自动重发，必须显示为需要处理；
- 飞书禁用或配置错误不得伪装成功。

pending_cleanup：

- 仅 NotificationDelivery.status=sent 时允许；
- 复用现有清理服务和字段；
- 已清理时重复执行直接成功；
- 清理失败时保留 cleanup_failed 与脱敏错误码；
- 飞书成功但清理失败后的重试只能清理，绝不能再次发送。

当前 Worker 中“发送成功后尝试清理、异常被吞掉并直接完成任务”的行为必须改为显式 pending_cleanup stage，但不增加新表。

## 8. Done Archive 规格

### 8.1 状态机与幂等

~~~text
done_discovery
→ done_capture_and_archive
→ archive_verify
→ enqueue_markdown_delivery
→ completed
~~~

hourly 增量扫描和 nightly 全量核对只为新增、变化、缺失或此前失败事项创建任务。

~~~text
done:{oa_item_key}:{discovery_hash-or-manifest-signature}:archive-v2
~~~

同一事项同一发现版本只能有一个活动任务。已达到归档完成标准且 manifest 未变化时，不再进入详情页或重复下载。

### 8.2 归档与验证

done_capture_and_archive 复用现有只读 CollaborationDetailAdapter 和归档实现，下载 OA 正文、附件和容器文件。它不得调用解析器、LLM、Curated、Online Audit 或 Knowledge 代码。

archive_verify 从本地事实检查：

- OAItem 已建立；
- 应归档附件已记录；
- 文件真实存在；
- 大小与记录一致；
- SHA256 存在且匹配；
- 相对路径位于允许的归档根；
- 第 10 层不存在尚未遍历的子级；
- 没有明确缺失附件。

有附件且全部通过时归档完成；OA 明确无附件时标记 no_attachment。需要遍历超过 10 层、缺失、哈希不符和不安全路径均不得显示完成。Markdown 状态不参与归档判断。

归档成功后创建独立 Markdown Delivery 协调任务并完成 Done Archive 任务，不再将同一个 Done 任务推进到解析或分类。

~~~text
markdown:{oa_item_key}:{archive-content-signature}:{markdown-schema-version}
~~~

archive-content-signature 由 OAItem 元数据哈希及已验证附件 ID、角色和 SHA256 的稳定排序结果计算。

### 8.3 失败恢复

- OA 登录过期可恢复并等待重新登录；
- 浏览器忙时释放当前尝试并退避；
- 下载超时从未验证附件继续；
- 已验证文件不重复下载或覆盖；
- 哈希不符时保留异常，不以新哈希静默接受；
- 第 10 层仍存在子级时记录 depth_limit_reached；
- 单事项失败不阻止其他事项。

## 9. Markdown Delivery 规格

### 9.1 状态机

~~~text
attachment_inventory
→ parse
→ source_publish
→ classify
→ index_publish
→ completed
~~~

该流水线只读取数据库和 data_root 下的本地文件。

attachment_inventory 读取已验证且属于允许 source role 的 ArchivedFile，仅为缺少当前有效产物或源 SHA256 已变化的文件创建或复用 ParseJob。无附件事项跳过解析，但仍执行分类和索引发布。已有成功且哈希有效的 Markdown 不重新解析。

### 9.2 Parser Router

| 文件类型 | 首选解析器 |
| --- | --- |
| Word、Excel、PPT | MarkItDown |
| 文本型 PDF | MarkItDown |
| 扫描、图片、复杂表格 PDF | MinerU |
| 图片 | MinerU |
| 明确不支持格式 | 终态 unsupported |

PDF 先进行轻量本地检测；MarkItDown 质量低于门限时允许转 MinerU。MinerU 不可用是可恢复错误，只影响相应文件。成功解析新增 ParseArtifact，验证产物哈希后才能切换 ContentObject.active_parse_artifact_id，不得覆盖旧 artifact。

### 9.3 唯一发布链

新产生的附件 Markdown 唯一链路为：

~~~text
ArchivedFile
→ ParseJob
→ active ParseArtifact
→ source_markdown/service.py
→ MarkdownExport
→ Source Markdown
~~~

必须停止生产入口：

~~~text
MarkdownExport
→ markdown_export/service.py
→ 再次调用 parse_file
~~~

source_publish 只能调用 publish_active_artifact。一个附件失败不得撤销其他附件已成功发布的结果。unsupported 不创建 ReviewEntry，而是在事项索引中显示“不支持转换”。可解析附件缺少有效 artifact 时保持失败或重试状态，不生成伪 Markdown。文件使用临时文件和原子替换发布，数据库只在文件存在且哈希验证成功后标记 success。

### 9.4 最小分类

1. 确定性规则优先判断 internal、external 或 unknown；
2. 内部事项从八个固定类别中选择；
3. 外部事项输出规范化发文机构；
4. 规则不足时允许本地 qwen3.5:9b 从受控结果中选择；
5. 模型不可用或低置信度时使用 unknown、其他内部或其他外部机构；
6. 只更新 OAItem 的四个字段；
7. 不创建历史、Review、关系、知识摘要或分类目录。

### 9.5 事项索引

每个 Done 事项生成：

~~~text
<Source Markdown 根>/<原始 OA 归档镜像路径>/_index.md
~~~

同目录保存附件 Markdown 和解析资产。_index.md 只包含 OA 事项标题、发起人、发起/办结时间、文号、四个分类字段、附件清单、成功附件 Markdown 的相对链接，以及 unsupported 或失败附件的状态。

索引页不得生成 AI 知识总结。无附件事项也必须生成索引。分类变化只更新 Frontmatter，不移动路径；内容未变化时不得改写文件时间。

Markdown Delivery 只有在全部可解析附件成功、unsupported 已明确记录且索引页成功时完成。临时失败可重试；不安全路径、原件哈希不符和 depth_limit_reached 显示为需要处理。Markdown 失败不得回退 Done Archive 的归档状态。

## 10. 调度、资源与重试

systemd 只自动运行：

- hourly：Pending 扫描和 Done 增量扫描；
- nightly：Done 全量清单核对；
- OA Worker：Pending 与 Done Archive；
- Markdown Worker：Markdown Delivery。

优先级：

~~~text
Pending > 新增 Done Archive > Markdown Delivery > 手工历史补齐
~~~

OA 浏览器操作共用现有独占租约；MinerU/GPU 仅由 Markdown Worker 使用；Pending 规则摘要不等待 GPU；LLM 或 MinerU 故障不得阻止 OA 扫描和原件归档；退役 stage 标记 RETIRED_STAGE，不再自动重试。

沿用现有 attempts、max_attempts、next_retry_at 和 lease。可恢复错误使用有上限的指数退避；进程崩溃后，过期 lease 回到原 stage；每次重试先检查输出事实，已完成则推进；非恢复错误不得循环重试。人工重试复用业务幂等键，不得删除或覆盖已验证原件。

## 11. WebUI 信息架构

### 11.1 导航

~~~text
总览
待办通知
已办归档
Markdown 输出
────────
设置
~~~

默认进入总览。设置固定在侧栏底部。AdvancedMaintenance 不得被生产前端导入或渲染。

### 11.2 总览

总览只显示待办通知、已办归档和 Markdown 交付三张卡，以及聚合的“需要处理”区域。

- 待办卡：最近/下次扫描、最近发现、新增/变化、摘要成功/规则兜底、飞书成功/失败/未知、待清理/清理失败。
- 归档卡：最近/下次扫描、最近新增、累计发现、完整归档、确认无附件、失败/需要处理、已验证附件。不得混入 Markdown 或 Curated。
- Markdown 卡：已发布事项索引、已发布附件、待解析、处理中、失败、unsupported、internal/external/unknown 数量、最近成功时间和 Source Markdown 根状态。

“最近一次”来自最近完成的 schedule run；“当前”来自任务和事实表；“累计”来自事实表。不可查询时返回 null 和 unavailable，不得以 0 伪装。

### 11.3 待办通知页

服务端分页，每页 50 条。列为事项、发起人、截止时间、摘要、飞书状态、清理状态和最近更新时间。

清理成功后 API 不得恢复业务字段，页面显示“业务内容已清理”及发送/清理时间。与 OA 同步只允许活动、未清理或发送失败事项。

筛选：全部、处理中、待发送、发送失败、结果未知、待清理、清理失败、已完成并清理。

操作：重试摘要、重试明确失败的飞书、与 OA 同步、重试清理。unknown_outcome 没有直接重发入口。

### 11.4 已办归档页

列为事项、发起时间、办结时间、附件数量、归档状态、Markdown 状态、本地相对目录和最近更新时间。

归档状态：待发现、归档中、完整归档、确认无附件、部分缺失、需要处理。

Markdown 状态：待处理、处理中、已交付、部分交付、交付失败、含不支持格式。

两类状态必须分开计算和显示，不得读取 Curated 决定状态。允许打开受边界校验的本地目录、重试归档、重建 Markdown 和查看简化详情。

### 11.5 Markdown 输出页

页面按 OA 事项聚合。列为事项、source_type、内部类别或外部机构、Markdown 数量、交付状态、Source Markdown 相对路径、最近更新时间和失败原因。

详情显示 _index.md、各附件 Markdown、不支持附件及失败/待重试附件。允许重试失败项、重建事项索引和打开 Source Markdown 目录。不允许在线编辑、Review、Curated/Vault 发布、知识生成或批量删除。

### 11.6 设置页

只保留：

- OA 登录状态、最近扫描和重新登录；
- 飞书启用与配置状态、最近成功/失败；
- LLM 启用、当前模型、Ollama 状态和规则兜底说明；
- MinerU 启用、服务状态和最近错误；
- data_root、归档根、Source Markdown 根及自动运行状态。

不得显示 Secret 值或提供测试飞书按钮。设置和服务开关必须经过现有 CSRF 门禁，且返回实际操作结果。

## 12. Web API

核心业务接口：

~~~text
GET  /api/simple-status

GET  /api/pending-notifications
GET  /api/pending-notifications/{id}
POST /api/pending-notifications/{id}/retry-summary
POST /api/pending-notifications/{id}/retry-delivery
POST /api/pending-notifications/{id}/retry-cleanup
POST /api/pending-notifications/{id}/sync

GET  /api/done-archives
GET  /api/done-archives/{id}
POST /api/done-archives/{id}/retry
POST /api/done-archives/{id}/rebuild-markdown
POST /api/done-archives/{id}/open-directory

GET  /api/markdown-outputs
GET  /api/markdown-outputs/{oa_item_id}
POST /api/markdown-outputs/{oa_item_id}/retry
POST /api/markdown-outputs/{oa_item_id}/rebuild-index
POST /api/markdown-outputs/{oa_item_id}/open-directory

GET  /api/settings
PUT  /api/settings
GET  /api/schedule
POST /api/schedule/control
~~~

GET 只读；POST/PUT 必须通过 loopback、认证和 CSRF 门禁。列表均服务端分页。长任务返回 202 和任务 ID；不满足前置条件时返回 409；错误始终返回 JSON。API 不返回 OA 正文、附件内容、模型输入输出、飞书正文、凭据、Cookie 或浏览器状态。列表返回相对路径；绝对根仅在设置页显示。

以下路由不再注册，旧地址返回 JSON 404：

~~~text
/api/audits/*
/api/governance/*
/api/reviews/*
/api/policies/*
/api/batches/*
/api/backfill/*
/api/lifecycle/knowledge*
/api/lifecycle/processing*
/api/maintenance/*
~~~

核心 schedule、登录和健康接口可以保留，但响应必须使用业务语言，不暴露完整 Queue 或 lease 明细。

## 13. Web 交互与安全

- 总览和列表每 5 秒静默刷新；搜索、筛选和分页状态不得丢失。
- 状态不能只依赖颜色。
- 绿色只表示业务结果成立；黄色表示处理中、等待重试、规则兜底或 unsupported；红色表示明确失败、未知投递结果或路径/哈希/深度问题。
- 页面刷新失败时保留上一次成功数据，并显示“数据可能已过期”。
- 打开目录时只能根据数据库记录解析目标，禁止接受任意用户路径。
- 不新增修改 OA 或发送测试飞书的按钮。

## 14. 实施阶段与门禁

### Phase 0：冻结基线与特征测试

确认工作树和用户修改；建立本地冻结标签；为三条链路补特征测试；在仓库外备份配置、数据库和 systemd unit；记录无业务标识的数量、字节数和哈希汇总。

门禁：未修改行为前全量测试通过，回滚路径已演练，Git 对象指向正确。

### Phase 1：断开非核心能力

移除非核心 Worker stage、自动入队、systemd 路径、Web 路由和前端入口；默认 CLI 不展示退役写入命令；增加只读 legacy CLI；停止领取退役任务。

门禁：连续 24 小时无非核心新写入，核心任务仍正常领取。

### Phase 2：修通 Pending Assistant

按 baseline、LLM 门禁、规则摘要、投递幂等、显式清理、API/UI 和崩溃恢复顺序实施。

门禁：同版本最多发送一次；模型关闭零调用；成功后彻底清理；清理失败只重试清理；Pending 不进入永久知识目录。

### Phase 3：拆分 Done Archive 与 Markdown Delivery

增加 archive_verify；删除 Done 对 Online Audit、Curated 和 Knowledge 的回调；验证成功后创建独立 Markdown 任务。

门禁：归档不受 Markdown 失败影响；已验证附件不重复下载；无附件和超过 10 层的口径正确。

### Phase 4：统一 Markdown 与最小分类

执行最小 Alembic 增量迁移；固化 Parser Router；停止旧直接解析发布入口；实现分类和 _index.md；按事项聚合交付状态。

门禁：历史成功不重解析；新附件 Markdown 均来自 active ParseArtifact；每个已归档事项恰好一个索引；分类不移动路径。

### Phase 5：轻量控制台

基于现有 Simple Views 改造；新增 Pending 和 Markdown 页面；分离归档/Markdown 状态；移除高级维护和退役 API。

门禁：导航、API、隐私、分页、状态和构建满足本规格。

### Phase 6：文档、部署与兼容收尾

更新 README、README.zh-CN、runbook、安全文档、示例配置、systemd 文档和 CLI 帮助。文档必须明确 Pending 短生命周期、Done 原件永久、ParseArtifact 唯一新发布链、Source Markdown 交付边界及退役能力只读兼容。

## 15. 测试策略

### 15.1 自动化测试

单元测试覆盖幂等键、baseline、规则摘要、分类枚举、PDF 路由、路径边界、Frontmatter、索引链接、状态聚合及错误可恢复性。

数据库迁移测试从上一 Alembic revision 创建合成数据库，验证新增字段默认值、现有行不变、MarkdownExport 约束、外键/索引及旧成功记录可读。

集成测试覆盖 Pending 全状态机、飞书成功后崩溃恢复、Done/Markdown 任务拆分、lease 恢复、Parser→Artifact→Markdown、无附件、多附件、unsupported、MinerU 不可用、哈希不符、退役 stage 及退役 Web 404。

OA、飞书、Ollama、MinerU 和 systemd 均使用可控 fake，不接触真实服务。

合成端到端测试运行 hourly enqueue、OA Worker、Markdown Worker 和 Web API，验证三条业务口径及产物。所有数据位于临时目录。

固定验证命令：

~~~bash
uv run python scripts/check_public_release.py
uv run pytest
cd webui
npm ci
npm run check
npm run build
~~~

前端构建后再次运行静态资源相关后端测试，确保源码与生产 bundle 一致。

### 15.2 真实环境只读验证

真实数据只在本机使用，验证结果不得进入 Git。部署前备份数据库、配置和 unit，记录脱敏汇总，暂停 timer，部署数据库增量、代码和前端，先启动 Web/Worker，再恢复 timer。

Pending：首次 baseline 不发送历史通知；仅观察自然产生的新待办或变化；不创建 OA 测试事项、不发送测试飞书。若自然通知发生，只核对计数、时间、一次性投递和清理结果。

Done：确认历史成功事项不重复下载；观察自然新增 Done；随机抽查 100 个事项的文件存在性、大小、SHA256、路径边界和深度状态。报告不得包含标题、OA 标识、文件名或绝对路径。

Markdown：确认历史成功不重解析；抽查 100 个事项索引、附件 Markdown、链接、Frontmatter 和 Obsidian 查询；确认 MinerU 不可用不阻塞其他流水线。

至少连续观察 7 天，并满足：

- 无重复飞书；
- Pending 清理后无业务内容残留；
- 已验证原件无覆盖或丢失；
- 成功 Markdown 无无故重建；
- Worker 重启后任务恢复；
- 非核心模块无新写入；
- 无持续增长的未知积压。

高风险问题出现时停止合并并恢复冻结版本；不得用删除业务数据的方式回滚。

## 16. 合并到 main

只有以下条件全部满足后才可合并：

- Phase 0–6 全部门禁通过；
- 固定验证命令成功；
- 7 天真实观察通过；
- git diff main...agent/oaradar-business-workflows 已人工复核；
- Git 候选集无机密或本地运行数据；
- README、runbook 与行为一致；
- 用户明确批准合并。

若 main 仍为 4a0eb40，优先 fast-forward；若已经前进，先审查新增提交再普通 merge，不得盲目 rebase 或覆盖。合并后在 main 再次完整验证。远程 agent 分支只有在 main 稳定且用户再次明确授权后才能删除。

## 17. 非目标

首个 V2 版本不实施：

- 旧代码或旧表物理删除；
- oa_knowledge 包名重命名；
- 新数据库或数据迁移平台；
- Curated 插件化；
- Embedding、全文索引或 AI 知识助手；
- 分类管理后台或 Review；
- 独立 Obsidian Vault 写入；
- llm_wiki wiki 写入；
- 任何 OA 写操作；
- 测试飞书发送。

## 18. 最终验收标准

1. 首次 baseline 不发送历史 Pending。
2. 同一 Pending 内容版本在重复扫描、重试和 Worker 重启下最多发送一次。
3. llm.enabled=false 时没有模型调用，并能生成规则摘要。
4. 飞书成功、清理失败后的恢复只执行清理。
5. 清理后不保留 Pending 正文、附件、快照或摘要。
6. Done 成功只由本地原件完整性决定，Markdown 失败不改变归档成功。
7. 已验证附件不会重复下载或覆盖。
8. 第 10 层仍有未遍历子级的事项永远不显示完成。
9. Markdown Worker 不访问 OA。
10. 新附件 Markdown 全部由 active ParseArtifact 发布。
11. 历史成功且哈希有效的 Markdown 不被升级过程重新解析。
12. 每个已归档事项有唯一 _index.md，无附件事项也不例外。
13. _index.md 只含来源元数据、分类和附件链接，不含 AI 知识总结。
14. 分类只影响 Frontmatter，不改变路径。
15. unsupported 不创建 ReviewEntry，也不无限重试。
16. WebUI 一级导航只有总览、待办通知、已办归档、Markdown 输出和设置。
17. 总览不读取 Curated、Review 或 Online Audit 决定核心状态。
18. 已清理 Pending 的 API 不泄露已删除业务字段。
19. 退役 Web 路由返回 JSON 404，退役 stage 不再执行。
20. Python 测试、前端类型检查、生产构建和公开候选集检查全部通过。
21. 真实环境只读观察连续 7 天通过。
22. 实施、验证和合并过程不修改 OA、不发送测试飞书、不提交任何真实 OA 数据。
