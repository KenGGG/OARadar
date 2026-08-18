# Security and privacy boundary

OARadar is designed for local, read-only use against an OA system. A public source repository must never become a transport or backup location for OA content.

## Data that stays local

Do not commit or publish:

- OA base URLs, deployment-specific paths, tenant or portal identifiers;
- usernames, passwords, API keys, tokens, cookies, authorization headers, or browser credential stores;
- item metadata, subjects, senders, workflow history, body content, attachments, or derived summaries;
- real HTML snapshots, screenshots, Playwright traces, downloads, databases, manifests, reports, logs, or generated knowledge-base files;
- browser profiles, local service files, machine-specific paths, or internal implementation reports.

The supplied `.gitignore` keeps these categories out of Git. Real configuration belongs in the ignored `config.yaml`; runtime content belongs under the ignored `data_root`, which defaults to `./data`.

Run the release check before every public commit:

```bash
uv run python scripts/check_public_release.py
```

The scanner examines tracked and untracked, non-ignored Git candidates. It reports only a path, line number, and rule name so that a discovered secret is not echoed to the terminal or CI log.

## OA behavior

OA integrations are read-only. Do not add actions that approve, reply, delete, forward, edit, or otherwise change OA records.

Container traversal is limited to depth 10. When an item has additional children, enqueue `depth_limit_reached`; do not report the item as complete.

OA identifiers are stored as text. Archive paths are stored relative to `data_root` so a database cannot expose machine-specific absolute paths.

## Credentials

Configuration loading rejects plaintext credential keys such as password, Cookie, Authorization, Token, and Secret. Use environment-variable names or a local browser credential mechanism. Do not pass credentials on the command line or write them to logs.

## Models and external services

Local processing is mandatory for OA-derived model input and LLM enrichment is disabled in the public example. The production configuration accepts only loopback Ollama with `qwen3.5:9b`; remote provider modes and endpoints are rejected.

## Tests and examples

Tests and documentation use only synthetic or irreversibly redacted values. Reserved domains such as `example.invalid` and documentation IP networks are preferred. Security tests that intentionally contain secret-like strings are marked `public-release: synthetic` and remain subject to code review.

## Reporting a vulnerability

Open a GitHub security advisory for a code vulnerability when repository security reporting is available. Do not include real OA content, credentials, endpoints, or internal identifiers in a public issue.
