# OARadar 运行手册（小时扫描 + 归档路径统一）

本手册对应 `docs/plan-0805-01.md` 的第七、八节与第四节“统一归档路径”。

## 1. 长期运行的进程

| 进程 | 命令 | 作用 |
| --- | --- | --- |
| OA 操作 worker | `oa worker --poll-seconds 2` | 处理实时/历史队列：抓取、附件、解析、摘要、通知 |
| Markdown worker | `oa markdown-worker --poll-seconds 2` | 把已验证附件转成 Markdown 知识文档 |
| （可选）llm_wiki | 见项目 wiki 模块 | 监听 `workspace/raw/sources/oa` 生成 `workspace/wiki` |

推荐用 `scripts/systemd/` 下的用户服务常驻运行（见其 README）。

## 2. 定时扫描

- 工作时段每小时：`pending discover`（全量待办，仅新增/变化入队）+ `manifest refresh-head --max-pages 3`（已办最新三页）。
- 每日夜间：`manifest sync` 全量核对，发现最新三页之外的异常、下载失败、清单不一致、已归档未 Markdown 化等问题。

`flock` 保证上一次扫描未结束时不会重叠启动。

## 3. 数据去向

```
待办新增/变化 → 本地摘要 → 飞书卡片（notify_feishu 阶段，幂等账本防重复）
已办新增      → 原始归档 → 立即入 Markdown 队列 → markdown-worker → llm_wiki 知识库
```

## 4. 统一归档路径（一次性迁移）

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
