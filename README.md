# OARadar

[English](README.md) | [简体中文](README.zh-CN.md)

OARadar is a local-first, read-only OA raw-file archive and faithful Markdown conversion system. It archives OA files without changing them and mirrors every source into Markdown for llm_wiki and Obsidian. OARadar does not build knowledge pages or write the llm_wiki `wiki/` directory.

The repository contains only application code and synthetic fixtures. OA addresses, credentials, records, attachments, browser state, databases, logs, and generated knowledge content stay on the operator's machine.

## Safety model

- OA access is read-only. The application does not approve, reply to, delete, forward, or alter OA records.
- Runtime state is written below the configured `data_root`, which defaults to the ignored local directory `./data`.
- Credentials are not accepted as YAML fields. Use local environment variables or the browser's local credential mechanism.
- Browser profiles, cookies, snapshots, downloads, databases, logs, and local configuration are excluded from Git.
- Container trees are traversed through depth 10. If more children exist, the item is queued as `depth_limit_reached` and is not reported complete.
- Tests use synthetic or irreversibly redacted fixtures only.
- Remote model calls remain disabled by default. Review confidentiality and redaction policy before enabling any remote provider.

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

The Web console is organized around the product's business chains. The primary navigation is Overview / Pending Notifications / Done Archives / Markdown Output, with Settings at the bottom of the sidebar. "Overview" aggregates the three automation chains (pending notification, done archive, Markdown delivery) and surfaces only the issues needing human attention. "Pending Notifications" is a short-lived notification console: once Feishu confirms a successful send, the business payload is erased and only a minimal de-duplication ledger is kept. "Done Archives" shows whether original attachments are fully, permanently saved and turned into Markdown. "Markdown Output" lists files successfully converted and handed off to the llm_wiki source directory. "Settings → Maintenance" provides the read-only online review, Markdown queue, PDF MinerU re-conversion, archive-date calibration, and local service controls. See [plan-0807-1](docs/plan-0807-1.md).

For browser login and read-only discovery, use the relevant `oa login`, `oa batch`, or `oa manifest` commands shown by `uv run oa --help`. Review the planned batch locally before any collection run.

## Archive by initiation time

Done-item raw files and their Markdown mirrors are organized by the OA initiation timestamp as
`raw/done/YYYY/MM/<item>`. The audit page exposes progress and controls for historical path
reconciliation. Items for which OA truly provides no initiation timestamp are placed under
`raw/done/unknown/`; completion and collection timestamps are never substituted. Reconciliation
only moves local artifacts and updates indexes—it does not mutate OA records or reconvert successful
Markdown content.

## Local document processing

The default configuration uses local processing and disables LLM enrichment. A loopback MinerU service can be started with:

```bash
docker compose -f mineru/docker-compose.yaml up -d mineru-api
```

Before enabling a remote model provider, verify that your organization permits it and keep `allow_confidential`, `allow_restricted`, and redaction controls aligned with your policy.

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
