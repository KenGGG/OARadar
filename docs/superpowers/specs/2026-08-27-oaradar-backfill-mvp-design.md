# OARadar 真实 OA Backfill MVP 设计

## 目标

下一项交付不是新的分类平台模块，而是 100 条真实 OA 的端到端候选成果：

```text
OA 输入 → 排除检查 → 元数据分类 → 必要时解析附件
→ needs_review → OA Package/Markdown → CSV/JSON 报告 → 对账
```

100 条验收通过后，同一入口直接扩展到全部 6144 条目标 OA。

## 范围约束

- 保留并复用现有 Phase 1 任务 1—6，不重写分类账、私有规则、完整性判断或解析缓存。
- 不开发抽样框架、抽样算法平台或新的抽样数据库结构。
- 暂缓附件内容分类规则、本地 Qwen、WebUI、正式发布、长期版本管理和复杂回滚。
- 原件只读；不得修改、移动、删除或覆盖 `data/originals/` 下的任何内容。
- 所有候选输出写入新的 `data/markdown/.builds/<run_id>/`，不创建或修改正式 `current`。
- 个别下载、完整性、解析或分类异常只进入报告，不阻塞其他 OA。

## 100 条选择

使用现有 manifest、OA 元数据和附件计数做一次确定性分层选择：

- 60—70 条普通样本，按稳定 OA key 排序后均匀选取；
- 30—40 条特殊样本，覆盖内部模板、外部来文、文件传阅、有/无文号、多附件、无附件、mixed 人员和已知异常；
- 同一 OA 只出现一次；特殊桶不足时由普通样本补足；
- 保存 `sample.csv`，包含 OA key、入选桶和简单依据，使相同输入可复现同一清单。

抽样结束后不增加人工确认步骤，立即执行端到端处理。

## 最小实现

新增一个窄边界的 Backfill MVP 服务和一个 CLI 入口。服务只负责连接现有能力：

1. 冻结 8119 条 manifest 快照，确认 1975 条 excluded 总闸门；
2. 从 6144 条 target 中选择 100 条或接受全量模式；
3. 对每条运行现有元数据规则；不能确定的直接保存为 `needs_review`；
4. 为分类所需或候选附件 Markdown 所需的文件复用现有解析器和内容 SHA 缓存；
5. 为每个选中 OA 创建一个候选 Package；无附件项只创建 `_index.md`；
6. 解析成功的附件忠实写入附件 Markdown，不用模型改写；解析失败时保留 Package，并写入异常报告；
7. 输出 `sample.csv`、`classification.csv`、`exceptions.csv`、`build_manifest.json`；
8. 校验选中 OA、Package、附件结果、异常和文件哈希能够完整对账。

不为 MVP 新建 build ledger。运行身份、输入摘要、文件清单和 SHA-256 由 `build_manifest.json` 保存；现有分类账继续记录分类决策。

## 候选目录

```text
data/markdown/.builds/<run_id>/
├── sample.csv
├── classification.csv
├── exceptions.csv
├── build_manifest.json
└── packages/
    ├── internal/<category>/<year>/<month>/<oa-package>/
    ├── external/<issuer>/<year>/<month>/<oa-package>/
    └── needs_review/<year>/<month>/<oa-package>/
```

`needs_review` 只允许出现在候选运行目录，不是正式发布目录。

每个 Package 至少包含 `_index.md`。完整性阻断或附件转换失败必须在首页明确展示，不虚构正文或附件内容。

## 对账与验收

`build_manifest.json` 至少报告：

- manifest 总数、excluded 数、target 数；
- 本次选中与实际处理 OA 数；
- 自动分类数、needs_review 数；
- OA Package 数；
- 待转换、转换成功、转换失败、跳过的附件数；
- 异常数及按错误代码汇总；
- 所有候选文件的相对路径、大小和 SHA-256；
- `selected = packages = classified + needs_review`；
- `attachment_attempted = converted + failed + skipped`。

首批验收以真实数量和实际候选文件为准，不以新增模块数或任务数为准。

## 测试和执行安全

- 单元测试只用虚构 OA 和附件。
- 先证明排除项不会解析或生成 Package、异常不会中止批次、重复内容只转换一次、无附件只生成首页、两条对账公式成立。
- 真实运行前备份数据库并记录备份位置；运行期间不访问 OA 写接口。
- 若单项失败，记录异常后继续；只有输出根不安全、原件可能被修改、数据库无法迁移或总账无法对账时才终止整批。
