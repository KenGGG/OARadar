# OARadar

[English](README.md) | [简体中文](README.zh-CN.md)

OARadar is a local-first, read-only OA workbench with three stable automations: Pending notifications, permanent Done archives, and Source Markdown delivery. It never writes the llm_wiki `wiki/` directory.

The repository contains only application code and synthetic fixtures. OA addresses, credentials, records, attachments, browser state, databases, logs, and generated knowledge content stay on the operator's machine.

## Safety model

- OA access is read-only. The application does not approve, reply to, delete, forward, or alter OA records.
- Runtime state is written below the configured `data_root`, which defaults to the ignored local directory `./data`.
- Credentials are not accepted as YAML fields. Use local environment variables or the browser's local credential mechanism.
- Browser profiles, cookies, snapshots, downloads, databases, logs, and local configuration are excluded from Git.
- Container trees are traversed through depth 10. If more children exist, the item is queued as `depth_limit_reached` and is not reported complete.
- Tests use synthetic or irreversibly redacted fixtures only.
- OA-derived model processing is restricted to loopback Ollama with `qwen3.5:9b`; remote model endpoints are rejected.

See [docs/security.md](docs/security.md) for the complete public repository boundary.

## Requirements

- Python 3.12 or later
- [uv](https://docs.astral.sh/uv/)
- Node.js 20 or later for the Web UI
- Google Chrome or another supported Chromium executable for collection
- Optional: Docker and a local GPU for MinerU

## Install

```bash
uv sync --extra dev
cp config.example.yaml config.yaml
```

Edit the ignored `config.yaml` and supply the URL and paths for your own OA installation. The values shipped in `config.example.yaml` use the reserved `.invalid` domain and cannot contact a real OA service.

Initialize local storage and inspect the configuration:

```bash
uv run oa init --config config.yaml
uv run oa doctor --config config.yaml
uv run oa status --config config.yaml
```

Convert the archive incrementally and inspect the export ledger:

```bash
uv run oa convert --config config.yaml
uv run oa convert --config config.yaml --item done:12345
uv run oa convert --config config.yaml --force
uv run oa rebuild-markdown --config config.yaml
uv run oa markdown-status --config config.yaml
```

The default source archive is `data/archive/raw/oa/`. Markdown is written only to `data/workspace/raw/sources/oa/` with the same tree and an appended `.md` suffix (for example, `报告.pdf` becomes `报告.pdf.md`). See [the llm_wiki and Obsidian integration guide](docs/llm-wiki-obsidian-integration.md).

Start the loopback-only Web console:

```bash
uv run oa web --config config.yaml
```

The primary navigation is Overview / Pending Notifications / Done Archives / Markdown Output / Settings. Pending data is short-lived: after confirmed Feishu delivery, its business payload is cleaned and only de-duplication facts remain. Done Archive proves original files locally; Markdown Delivery then independently creates ParseArtifact-backed Source Markdown and one `_index.md` per item. Curated, governance, audit, review, policy, and backfill features are retained only as historical code/data compatibility and do not run in the V2 production chain.

For browser login and read-only discovery, use the relevant `oa login`, `oa batch`, or `oa manifest` commands shown by `uv run oa --help`. Review the planned batch locally before any collection run.

## Archive by initiation time

Done-item raw files and their Markdown mirrors are organized by the OA initiation timestamp as
`raw/done/YYYY/MM/<item>`. The audit page exposes progress and controls for historical path
reconciliation. Items for which OA truly provides no initiation timestamp are placed under
`raw/done/unknown/`; completion and collection timestamps are never substituted. Reconciliation
only moves local artifacts and updates indexes—it does not mutate OA records or reconvert successful
Markdown content.

## Local document processing

The default configuration uses local processing and disables LLM enrichment. When enabled, text enrichment uses only the installed local `qwen3.5:9b`. A loopback MinerU service can be started with:

```bash
docker compose -f mineru/docker-compose.yaml up -d mineru-api
```

Pending summaries can use local Ollama when enabled and otherwise use a deterministic rule fallback. New attachment Markdown is published only from an active, valid ParseArtifact; it is never reparsed from the original during publishing.

## Development

Run the Python tests:

```bash
uv run pytest
```

Build the Web UI:

```bash
cd webui
npm ci
npm run check
npm run build
```

Check the exact Git candidate set for sensitive or local-only material:

```bash
uv run python scripts/check_public_release.py
```

The same checks run in GitHub Actions. A finding must be removed or replaced with a clearly synthetic fixture; do not suppress real environment values.

## Important limitations

OA products and deployments differ. The included selectors and adapters may require local configuration or code adaptation. Test with a small, explicitly reviewed sample before scaling collection. OARadar is not a backup of the OA service and does not guarantee regulatory or records-management compliance.

## License

MIT License. See [LICENSE](LICENSE).
