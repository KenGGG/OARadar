# OARadar

OARadar is a local-first, read-only toolkit for archiving OA work items and attachments, validating the archive, extracting searchable content, and publishing a local knowledge base. It includes a Python CLI, a local Web console, SQLite migrations, browser-based collectors, document parsing, and synthetic tests.

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

Start the loopback-only Web console:

```bash
uv run oa web --config config.yaml
```

For browser login and read-only discovery, use the relevant `oa login`, `oa batch`, or `oa manifest` commands shown by `uv run oa --help`. Review the planned batch locally before any collection run.

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
