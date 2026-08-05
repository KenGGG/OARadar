# OARadar, llm_wiki, and Obsidian

The three applications collaborate through one local filesystem workspace. OARadar is the read-only OA collector and faithful converter; llm_wiki ingests converted Markdown and owns its knowledge base; Obsidian is the viewing and editing workspace.

## Directory contract

```text
data/
├── state/oa.db
├── archive/raw/oa/{pending,done}/
├── parse/{artifacts,staging,failed}/
└── workspace/
    ├── raw/sources/oa/{pending,done}/
    └── wiki/
```

OARadar exclusively maintains `workspace/raw/sources/oa/`. It never writes `workspace/wiki/` or `.llm-wiki/`. A source such as `archive/raw/oa/done/2026/07/OA-123/attachments/报告.pdf` maps to `workspace/raw/sources/oa/done/2026/07/OA-123/attachments/报告.pdf.md`; nearby resources use `报告.pdf.assets/` and relative links.

## OARadar

```bash
uv run oa init --config config.yaml
uv run oa convert --config config.yaml
uv run oa markdown-status --config config.yaml
```

Conversion is incremental by source SHA-256, engine identity/version, parser configuration hash, and Markdown schema. Use `--force` for explicit regeneration or `rebuild-markdown` for a full ledger-driven rebuild. Failures retain the last successful Markdown; a first failure or unsupported type produces an explicit placeholder.

## llm_wiki

Set the llm_wiki project directory to `data/workspace/`. Configure Folder Import or Source Watch for `raw/sources/oa/` and generate its own knowledge base in `wiki/`. llm_wiki should ingest the Markdown and does not need to parse the original PDF, Word, Excel, or PowerPoint files again.

## Obsidian

Open `data/workspace/` as the Vault. The file tree then shows:

```text
raw/sources/oa/   OARadar converted source Markdown
wiki/             llm_wiki generated knowledge base
```

Obsidian is the viewing/editing interface. Do not configure OARadar to publish user notes or generated knowledge pages outside `raw/sources/oa/`.
