# OA Markdown V1 分类与生成设计

## 1. 目标与范围

本设计在已办 OA 原件归档完成后，建立一条可重复调用的本地后处理链路：

1. 判断事项是否具备后处理条件；
2. 以 OA Package 为单位进行来源和业务分类；
3. 仅在元数据规则不足时按需解析附件；
4. 仅在确定性规则仍不足时调用本机 Qwen；
5. 通过 Dry Run、人工复核、构建 QA 和数量对账后发布 Markdown；
6. 为以后新增 OA 复用同一套服务接口。

本阶段不建设摘要、向量数据库、RAG、知识图谱、外部联网查询或复杂 Agent 工作流。

## 2. 不可违反的约束

- `data/originals/` 永远只读；不得修改、移动、覆盖或删除原件。
- OA 集成保持只读，不对 OA 记录执行审批、回复、删除、转发或修改。
- `data/` 顶层只保留 `originals/` 与 `markdown/` 两个目录。
- 真实人员配置、OA 标题、附件内容、Dry Run 明细和运行数据不得进入 Git。
- 测试只能使用虚构或不可逆脱敏数据。
- OA 标识始终按文本保存。
- 容器遍历深度最多为 10；达到上限且仍有子项时不得报告完成。
- 正式 Markdown 中不得出现 `unclassified` 或 `needs_review` 目录。
- 人工锁定的分类结论不得被规则或模型自动覆盖。

## 3. 权威数据源

数据库中的 `oa_manifest_items` 是处理目标和状态的权威来源。`oa_manifest.csv` 是可审计导出，不作为程序运行时的主输入，避免因 CSV 过期造成重复、遗漏或状态倒退。

每次全量 Dry Run 冻结一个目标集合和输入签名。输入签名至少包含：

- 目标 OA key 集合；
- manifest 状态与排除规则版本；
- 分类规则版本；
- 私有配置内容的 SHA-256；
- 分类 schema 版本；
- 本机模型名称和提示词版本。

增量任务与全量任务调用同一套服务，只是目标集合不同。

## 4. 总体数据流

```text
OA 下载达到终态
  ↓
ArchiveReadinessService
  ├─ skipped → classification_status=excluded
  ├─ confirmed empty → integrity=no_attachment_confirmed
  ├─ verified files → integrity=ok
  └─ missing/hash/download failure → 记录完整性异常并阻止发布
  ↓
读取已有人工锁定决策
  ├─ manual_locked → 保留为当前有效结论
  └─ 无人工锁定 → 元数据规则分类
                         ├─ 确定 → 保存规则决策
                         └─ 不确定/冲突 → 按需解析必要附件
                                              ├─ 内容规则确定 → 保存规则决策
                                              └─ 仍不确定 → 本机 Qwen
                                                                ├─ 高置信且无冲突 → 保存模型辅助决策
                                                                └─ 其他 → needs_review
  ↓
Dry Run WebUI 复核与人工锁定
  ↓
MarkdownBuildService 构建 .builds/<run_id>
  ↓
PublicationService 数量对账、QA、人工确认和正式切换
```

解析不是全量分类的前置步骤。程序必须先运行纯元数据规则，只有无法确定的 OA 才能申请解析附件。相同内容 SHA-256 只能生成一份有效 ParseArtifact。

## 5. 两条独立状态轴

### 5.1 分类状态

`classification_status` 只表达分类结论：

- `classified`
- `needs_review`
- `excluded`

### 5.2 内容完整性状态

`content_integrity_status` 独立表达内容是否可用于转换和发布：

- `ok`
- `no_attachment_confirmed`
- `missing`
- `size_mismatch`
- `sha256_mismatch`
- `download_failed`
- `not_checked`

完整性异常不等同于分类错误。事项可以仅依据元数据得到 `classified`，但完整性不满足发布门禁时不得生成正式 Markdown。

## 6. 分类决策模型

分类决策是事项级、版本化记录。现有 `oa_items` 分类字段仅作为当前有效结论的查询快照，历史和证据以版本化决策表为准。

每条决策至少保存：

```text
oa_item_key
classification_status
content_origin
flow_type
initiator_type
relay_from
transfer_chain
issuer
business_category
document_number
document_type
normalized_title
decision_source
classification_confidence
classification_reason
rule_version
private_config_sha256
manual_locked
supersedes_decision_id
created_at
```

字段约束：

- `content_origin` 为 `internal` 或 `external`。
- `business_category` 仅适用于 internal；external 时必须为空。
- external 必须具有完整、规范化的 `issuer`。
- issuer 不得等于人员角色表中的任何人员标识或别名。
- `relay_from` 是便捷查询字段，不替代完整转发链。
- `transfer_chain` 是有序结构，每一级保存人员标识、顺序、角色类型和证据来源，可表达多级转发。
- `decision_source` 为 `metadata_rule`、`content_rule`、`local_qwen` 或 `manual`。
- 分类理由保存结构化规则代码和必要证据，不复制附件全文或完整模型输入。

## 7. 人工决策

人工确认产生新的不可变决策版本，并设置：

```text
decision_source=manual
manual_locked=true
```

后续重跑可以计算新的系统建议并在 WebUI 展示差异，但不得改写当前人工结论。改判或解锁必须由人工明确操作，并产生新的决策版本，保留先前历史。

如果人工已分类的事项后来命中新排除策略，系统保留人工分类历史，但停止发布并在 WebUI 报告策略冲突。

## 8. 分类证据优先级

自动分类按以下顺序评估，高优先级证据可以覆盖低优先级默认值：

1. manifest 排除状态；
2. 正式文号或明确发文机关；
3. 外部公文流转模板；
4. 明确内部工作流模板；
5. 结构化转发链；
6. 发起人角色；
7. 标题语义；
8. 按需解析后的附件正文；
9. 本机 Qwen；
10. 人工审核。

禁止使用“标题包含银行/政府/公司”等单个普通词直接判断来源。人员角色是默认证据，不得覆盖更明确的文号、模板、发文机关或转发链证据。

自动发布的最低置信度默认为 `0.85`。正式文号映射、明确标准工作流等确定性规则可以产生更高置信度，但任何高优先级证据冲突都必须进入 `needs_review`，不得用分数相加掩盖冲突。阈值属于版本化分类配置，变更后必须产生新的运行签名和决策版本。

## 9. 标题标准化

分类前生成 `normalized_title`，只移除明确的版次噪声、日期提醒包装、重复空格和无意义序号包装。传阅标记、正式文号和转发关系在分类及 transfer chain 提取完成前不得删除。

用于内容去重的 canonical title 是第二个派生值，只能在分类证据提取完成后生成。原始标题始终保留，不被派生标题覆盖。

## 10. 内部业务目录

internal 事项必须归入以下一个目录：

```text
01_公司治理与决策
02_业务项目与投放租后
03_风险合规审计法务
04_财务资金与融资
05_经营计划与绩效考核
06_人力资源
07_党建纪检与工会
08_行政采购与信息化
09_对外报送与监管反馈
99_其他内部
```

内部分类遵循流程强规则优先。来源判断和业务分类是两个步骤：只有确认 internal 后，融资、银行、合同等业务词才参与业务目录判断。

## 11. 外部发文单位

external 事项以真实发文单位作为目录，不使用发起人、转发人或泛化机构词。识别顺序为：

1. 正式文号映射；
2. 正文正式发文机关；
3. 标题明确发文机关；
4. 文件首页、红头或落款；
5. 本机 Qwen 提取；
6. 人工确认。

文种只写入 metadata，不作为目录层级。

## 12. 私有规则配置

真实人员与机构映射存放在本地忽略目录：

```text
private/classification/initiator_profiles.yaml
private/classification/document_number_issuers.yaml
private/classification/title_templates.yaml
```

要求：

- 文件权限为 `0600`；
- Git 仅保存 schema 和虚构示例；
- `.env` 只允许保存私有规则目录路径，不保存人员列表；
- 启动时校验路径、权限和 schema；
- 配置缺失或无效时分类任务失败关闭，不退回宽松默认规则；
- 分类运行只持久化私有配置 SHA-256，不把完整配置复制到普通日志；
- 包含真实人员的 Dry Run CSV 和审核信息只存本地状态目录或数据库。

## 13. OA Package 与内容去重

一个 OA 事项及其正文、直接附件和关联容器附件构成一个 OA Package。分类首先作用于 Package，附件默认继承事项分类。附件如有独立正式发文单位，可保存附件级 metadata，但不拆散 OA Package 的来源关系。

必须区分 OA occurrence 与 canonical document：

- 每条 OA occurrence 均保留独立 Package 和来源关系；
- 内容 SHA-256 相同的附件复用 ContentObject 和 ParseArtifact；
- 正式文号与规范化标题可作为 canonical identity 的补充证据；
- 不因去重删除任何原始 OA 归档记录；
- 派生 Markdown 可以在不同 Package 中物化，但不得重复执行昂贵解析。

## 14. 按需解析与本机 Qwen

ParseCacheService 只接受已经通过原件完整性校验的文件。分类器为不确定事项选择必要来源，并按 ContentObject/SHA-256 请求解析。

本机 Qwen 仅处理内容规则仍无法确定的事项。允许输入：

- OA 标题；
- 发起人角色和转发链；
- 文号；
- 附件文件名；
- 首页或正文前若干千字；
- 已提取的结构化内容证据。

禁止发送到联网模型。模型必须返回严格结构化结果；低置信、issuer 不完整、字段约束失败或证据冲突均进入 `needs_review`。

## 15. Dry Run 与 WebUI

Dry Run 分三轮收敛：

1. 元数据规则轮；
2. 仅对不确定事项执行的内容规则轮；
3. 仅对仍不确定事项执行的本机 Qwen 轮。

WebUI“已办资料”增加分类预览与筛选：

- internal、external、needs_review、excluded；
- metadata rule、content rule、local Qwen、manual；
- mixed 角色、issuer 未识别、规则冲突、完整性异常；
- 结构化理由、置信度和多级 transfer chain；
- 人工确认、改判、锁定和建议差异；
- 当前筛选结果 CSV 导出。

Dry Run 汇总至少包含分类状态、来源类型、内部目录、外部 issuer、人员角色、需要解析数、实际解析数、预计与实际 Qwen 调用数、冲突数、issuer 未识别数和 canonical document 去重数。

## 16. 正式目录与 Package 唯一性

预发布内容位于：

```text
data/markdown/.builds/<run_id>/
```

正式内容位于：

```text
data/markdown/internal/<business_category>/YYYY/MM/<package>/
data/markdown/external/<issuer>/YYYY/MM/<package>/
```

Package 目录名必须包含由 OA key 稳定生成的不可逆短标识：

```text
YYYYMMDD-规范化事项标题--oa_<sha256(oa_item_key)短标识>
```

禁止使用 `(1)`、`(2)` 等临时重名后缀。每个可发布 OA key 必须且只能对应一个 Package。

普通 Package 至少包含：

```text
_index.md
正文.md（存在正文时）
附件01_<安全文件名>.md
附件02_<安全文件名>.md
```

`no_attachment_confirmed` 事项只生成 `_index.md`，不得尝试生成不存在的正文或附件 Markdown。

## 17. Frontmatter

事项索引至少保存：

```text
oa_item_key
title
normalized_title
content_origin
flow_type
initiator
initiator_type
relay_from
transfer_chain
issuer
document_number
document_type
business_category
canonical_document_ids
source_oa_ids
classification_method
classification_reason
classification_confidence
classification_decision_id
content_integrity_status
oa_completed_at
markdown_generated_at
```

附件 Markdown 额外保存原件文件 ID、内容 SHA-256、ParseArtifact ID、解析器版本和附件在 Package 中的角色。

## 18. 发布门禁与数量对账

可发布事项必须同时满足：

- `classification_status=classified`；
- internal 具有合法 business category；
- external 具有完整 issuer，且 issuer 不是人员标识；
- `content_integrity_status` 为 `ok` 或 `no_attachment_confirmed`；
- 不存在未解决的规则冲突；
- 当前有效决策已冻结到本次构建输入。

切换前必须证明所有目标事项完整拆分为互斥集合，例如：

```text
total = excluded + publishable + integrity_blocked + needs_review
```

同时验证：

- publishable 数量等于 `.builds/<run_id>` 中 `_index.md` 数量；
- 每个 published OA key 恰好对应一个 Package；
- 构建中不存在未知 OA key、重复 OA key 或遗漏 OA key；
- 不存在 `unclassified` 或 `needs_review` 路径；
- Markdown、资源文件、相对链接、frontmatter 和构建 manifest 均通过校验。

只有 WebUI 人工明确确认后，PublicationService 才能执行正式切换。

## 19. 版本切换、回滚和保留

`.previous/` 默认永久保留，不自动清理，并且只保存曾经正式发布成功的构建。

- 删除历史版本必须由 WebUI 人工明确选择；
- 当前正式版本和正在使用的回滚目标禁止删除；
- 回滚只能通过 PublicationService 执行；
- 回滚前的正式版本仍进入 `.previous/`，支持再次恢复；
- 删除 `.previous/` 只能删除 Markdown 派生数据；
- 删除不得影响 originals、分类历史、人工决策或解析缓存。

由于构建目录位于 `data/markdown/` 内部且 `data/` 顶层不得出现第三个目录，整个 Markdown 根目录无法通过一次祖先/子目录重命名完成切换。PublicationService 因此采用持久化两阶段切换：先冻结并校验构建，再用同文件系统原子重命名逐个替换正式分类子树，最后原子写入活动版本提交标记。切换日志记录每一步的旧路径、新路径和恢复动作；WebUI 只有看到提交标记后才报告新版本正式生效。任何中断都必须自动前滚或回滚到一个完整版本，禁止报告或继续使用无法解释的混合目录。

## 20. 服务边界

### ArchiveReadinessService

统一计算归档和完整性事实。下载 worker、人工重查和分类任务均调用该服务，不自行拼装状态。

### ClassificationService

执行元数据规则、内容规则、本机 Qwen 和人工决策解析；维护版本化决策，但不解析文件、不发布 Markdown。

### ParseCacheService

按内容哈希调度和复用解析产物；记录解析错误，但不决定事项分类。

### MarkdownBuildService

只从冻结的分类决策和有效 ParseArtifact 构建 `.builds/<run_id>`，构建期间不得临时重新分类。

### PublicationService

负责数量对账、目录唯一性、QA、人工确认、正式切换、回滚和历史派生版本清理。

## 21. 失败处理与重跑

- 每个阶段使用不可变目标集合和输入签名；
- 重跑从数据库事实恢复，不依赖列表偏移量；
- 单项失败不阻塞其他事项，但会阻止该事项发布；
- 配置变化、原件变化、ParseArtifact 变化或人工决策变化会产生新版本；
- 相同输入签名的成功阶段幂等跳过；
- 失败不得覆盖最后一次成功的正式 Markdown；
- 任务完成必须由持久化终态证明，不能只依据子进程退出码。

## 22. 测试与验收

自动化测试只使用虚构人员、机构、标题和附件，至少覆盖：

- manifest 排除优先；
- 元数据确定时不解析附件；
- 只有不确定事项按需解析；
- 内容规则仍不确定时才调用本机模型；
- 多级转发链；
- internal/external 字段互斥；
- 人工锁定不可自动覆盖；
- 同 SHA-256 只解析一次；
- 完整性状态与分类状态相互独立；
- 无附件事项只生成 `_index.md`；
- 稳定 OA 短标识和同日同标题冲突；
- 每个 published OA key 恰好对应一个 Package；
- 全量数量对账；
- 构建中断不影响正式目录；
- 切换失败恢复；
- 回滚可再次恢复；
- 历史删除仅影响派生 Markdown；
- Git 追踪文件中不存在私有人员配置或真实 OA fixture。

正式发布前还必须完成 Dry Run 汇总复核、mixed 角色抽样、external issuer 抽样、规则冲突复核、无附件抽样、目录与链接验证、磁盘容量检查和人工切换确认。

## 23. 实施边界

实施必须按以下依赖顺序拆分计划：

1. 私有配置 schema、分类决策和构建账本数据模型；
2. ArchiveReadinessService 与元数据规则；
3. ParseCacheService 按需解析；
4. 内容规则、本机 Qwen 兜底和人工锁定；
5. Dry Run API、WebUI 筛选、人工复核和 CSV；
6. MarkdownBuildService、唯一 Package 路径和 QA；
7. PublicationService 切换、回滚与历史版本管理；
8. 全量 Dry Run、抽样验收和首次人工发布。

在 Dry Run 和人工 QA 通过前，不执行首次正式 Markdown 切换。
