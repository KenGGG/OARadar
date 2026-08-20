# OARadar

[English](README.md) | [简体中文](README.zh-CN.md)

OARadar 是本地优先、只读访问 OA 的个人工作台，稳定运行三条自动化：待办通知、已办永久归档与 Source Markdown 交付；不会写入 llm_wiki 的 `wiki/` 目录。

本仓库仅包含应用代码和合成测试夹具。OA 地址、凭据、业务记录、附件、浏览器状态、数据库、日志和生成的知识内容均保留在操作人员本机。

## 安全模型

- OA 访问严格只读；应用不会审批、回复、删除、转发或修改 OA 记录。
- 运行状态写入配置的 `data_root`，默认是已被 Git 忽略的本地目录 `./data`。
- YAML 配置不接受明文凭据；请使用本地环境变量或浏览器的本地凭据机制。
- 浏览器配置文件、Cookie、快照、下载文件、数据库、日志和本地配置均不会进入 Git。
- 容器树最多遍历 10 层；如仍有子节点，将事项加入 `depth_limit_reached` 队列，且不会错误报告为完成。
- 测试只使用合成或不可逆脱敏的夹具。
- OA 派生内容只允许发送到回环地址上的 Ollama `qwen3.5:9b`；远程模型端点会被拒绝。

完整的公开仓库边界见 [安全文档](docs/security.md)。

## 环境要求

- Python 3.12 或更高版本
- [uv](https://docs.astral.sh/uv/)
- Web UI 需要 Node.js 20 或更高版本
- 采集需要 Google Chrome 或其他受支持的 Chromium 可执行文件
- 可选：用于 MinerU 的 Docker 和本地 GPU

## 安装

```bash
uv sync --extra dev
cp config.example.yaml config.yaml
```

编辑已被 Git 忽略的 `config.yaml`，填写你自己的 OA 地址和路径。`config.example.yaml` 使用保留的 `.invalid` 域名，不会连接真实 OA 系统。

初始化本地存储并检查配置：

```bash
uv run oa init --config config.yaml
uv run oa doctor --config config.yaml
uv run oa status --config config.yaml
```

增量转换归档并检查导出账本：

```bash
uv run oa convert --config config.yaml
uv run oa convert --config config.yaml --item done:12345
uv run oa convert --config config.yaml --force
uv run oa rebuild-markdown --config config.yaml
uv run oa markdown-status --config config.yaml
```

默认原始归档目录为 `data/archive/raw/oa/`。Markdown 只写入 `data/workspace/raw/sources/oa/`，保持原目录结构，并在原文件名后追加 `.md`（例如 `报告.pdf` 转换为 `报告.pdf.md`）。详见 [llm_wiki 与 Obsidian 集成指南](docs/llm-wiki-obsidian-integration.md)。

启动仅监听回环地址的 Web 控制台：

```bash
uv run oa web --config config.yaml
```

Web 控制台一级导航固定为「总览 / 待办通知 / 已办资料 / Markdown 输出 / 系统设置」。待办是短生命周期数据：飞书确认成功后清理业务内容，只保留去重事实；已办归档只证明原件完整；Markdown 交付独立从有效 ParseArtifact 发布 Source Markdown，并为每个事项生成一个 `_index.md`。Curated、治理、审计、Review、Policy 和 Backfill 仅保留历史代码/数据兼容，不再进入 V2 生产链。所有 OA 交互严格只读。

判定口径（重要）：

- “已完成”必须同时满足**原件已验证 + 事项 `_index.md` 已交付**；分类只更新 Frontmatter，不移动归档或 Markdown 路径。
- 待办每小时 05 分检查；夜间全量扫描每日 23:30。
- 模型“确定性兜底”**不等于**“模型成功”：兜底计数单独展示，不计入 qwen 成功数。
- 复杂诊断位于「系统设置 → 高级维护」；OA 正文、附件名与凭据不出现在默认页面。

详见 [极简 WebUI 设计规格](docs/superpowers/specs/2026-08-17-webui-simplification-design.md) 与 [实施计划](docs/superpowers/plans/2026-08-17-webui-simplification-implementation.md)。

如需浏览器登录和只读发现，请使用 `uv run oa --help` 中列出的 `oa login`、`oa batch` 或 `oa manifest` 命令。执行任何采集前，请先在本地审查计划批次。

## 按发起时间归档

已办事项的原始文件与 Markdown 镜像统一按 OA“发起时间”保存为
`raw/done/YYYY/MM/<事项>`。审计页的“发起时间归档校准”可查看并控制历史目录迁移；
若 OA 确实没有发起时间，事项会进入 `raw/done/unknown/`，系统不会用办结时间或采集时间
冒充发起时间。校准仅移动本地文件并更新索引，不会修改 OA 记录，也不会重新转换已成功的
Markdown 内容。

## 本地文档处理

默认配置使用本地处理并关闭 LLM 内容加工。可以通过以下命令启动仅监听回环地址的 MinerU 服务：

```bash
docker compose -f mineru/docker-compose.yaml up -d mineru-api
```

待办摘要在启用本地模型时使用 Ollama，关闭或失败时使用确定性规则兜底。新附件 Markdown 只允许从 active ParseArtifact 发布，发布时不会重新解析原件。

## 开发

运行 Python 测试：

```bash
uv run pytest
```

构建 Web UI：

```bash
cd webui
npm ci
npm run check
npm run build
```

检查即将纳入 Git 的候选文件中是否包含敏感或仅限本地的内容：

```bash
uv run python scripts/check_public_release.py
```

GitHub Actions 会执行相同检查。如发现问题，必须删除相关内容或替换为明确的合成夹具；不得通过抑制规则隐藏真实环境数据。

## 重要限制

不同 OA 产品和部署方式之间存在差异，仓库内的选择器和适配器可能需要本地配置或代码调整。扩大采集规模前，请先使用少量且经过明确审查的样本进行测试。OARadar 不是 OA 系统的完整备份，也不承诺满足监管或档案管理合规要求。

## 许可证

MIT License，详见 [LICENSE](LICENSE)。
