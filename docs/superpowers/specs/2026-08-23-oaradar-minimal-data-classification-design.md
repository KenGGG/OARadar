# OARadar minimal data and local classification design

Date: 2026-08-23  
Status: awaiting user review  
Baseline: `main@cb32336`

## 1. Outcome

OARadar exists to run only three business flows:

1. notify the operator about Pending items;
2. download and preserve original Done attachments;
3. convert those attachments to Markdown and classify the Markdown.

The `data/` directory is a business deliverable, not an application runtime
directory. After migration it contains exactly two top-level directories:

```text
data/
├── originals/
└── markdown/
```

This design supersedes the data layout, manual classification page, full
online-audit gate, clean-database rebuild, and `data_rebuilt/` cutover design in
`2026-08-21-oaradar-data-cleanup-markdown-rebuild-design.md`. OA remains
strictly read-only.

## 2. Storage contract

### 2.1 `data/originals/`

`originals/` contains only immutable files downloaded from OA as Done evidence:

- direct attachments;
- official attachments;
- an official body file when OA exposes it as a downloaded file;
- the original downloaded container file, such as a ZIP archive.

Page snapshots, workflow snapshots, metadata snapshots, extracted container
members, parser products, reports, logs, backups, quarantine files, browser
state, database files, and knowledge projections are not originals.

Every original must have a database ledger entry containing its OA item key,
role, size, SHA256, and POSIX path relative to `data_root`. OA identifiers remain
text. Original files are never edited or overwritten.

### 2.2 `data/markdown/`

`markdown/` contains only final Markdown, one `_index.md` per Done item, and
assets actually referenced by those Markdown files. It contains no parser JSON,
debug output, prompt/response capture, or abandoned versions.

The classification tree is:

```text
markdown/
├── internal/<fixed-category>/...
├── external/<normalized-issuer>/...
└── unclassified/...
```

The fixed internal categories remain:

```text
公司治理  经营管理  业务项目  风险管理
财务资金  人力行政  信息化    其他内部
```

`unclassified/` is a valid, non-blocking result. Classification is also written
to frontmatter and `_index.md` so search does not depend only on folders.

### 2.3 Runtime state outside `data/`

Operational files move to standard local-only directories:

```text
~/.local/state/oaradar/
├── oa.db
└── private-backups/

~/.cache/oaradar/
├── browser-profile/
└── work/
```

Logs use the systemd journal. Temporary parse products live below
`~/.cache/oaradar/work/` and are removed immediately after an atomic Markdown
publish succeeds. Failed work retains only the minimum file needed for retry and
is removed after the configured failure-retention period.

The state database remains necessary for Pending deduplication, notification
outcomes, original-file hashes, download idempotency, Markdown idempotency, and
classification versions. Moving it out of `data/` does not make it optional.

## 3. Minimal production pipeline

### 3.1 Pending

```text
discover -> summarize -> Feishu -> erase temporary business content
```

Only an explicit `sent` result permits cleanup. `unknown_outcome` is never
resent or cleaned automatically. The durable state database retains only the
minimum deduplication and delivery facts.

### 3.2 Done originals

```text
discover -> download -> verify size and SHA256 -> publish immutable original
```

Each file is processed independently. One missing or mismatched file does not
block other items. Container traversal stops at depth 10; an item with deeper
children is recorded as `depth_limit_reached` and is never reported complete.

Historical processing trusts local evidence only after rechecking the current
file size and SHA256. It does not require a new full OA audit and does not
require legacy originals to pass through an intermediate canonical directory.

### 3.3 Markdown and classification

```text
verified original
-> bounded temporary parse
-> item-level local classification
-> atomic classified Markdown publish
-> delete temporary parse product
```

Markdown generation is per attachment. Unsupported and failed attachments are
listed explicitly in `_index.md`; they do not roll back successful attachments.
Classification failure publishes the item under `unclassified/` and never
blocks Markdown delivery.

## 4. Local Qwen classification

The configured model is local Ollama `qwen3.5:9b`. Although the installed model
currently reports a larger maximum window, the classifier never relies on a
long-context request.

### 4.1 Fixed request budgets

- chunk map input: at most 2,000 estimated tokens;
- item reduction input: normally 6,000 and never more than 8,000 estimated
  tokens;
- model output: at most 512 tokens;
- safety margin: at least 1,024 tokens;
- concurrency: one local model request at a time.

The existing conservative mixed Chinese/ASCII token estimator is applied before
every request. Ollama receives an explicit bounded `num_ctx`; an oversized input
is split locally and is never sent optimistically.

### 4.2 Rule-first hierarchical classification

1. Deterministic rules extract candidates from the OA title, sender, document
   number, attachment names, file roles, and known issuer aliases.
2. Each attachment Markdown is split on paragraph boundaries. Qwen maps every
   chunk to a compact structured signal summary containing only category,
   issuer, document-number, project, and evidence signals.
3. Large sets of chunk summaries are reduced in bounded groups until one compact
   summary remains per attachment.
4. Attachment summaries are reduced again to one item-level summary.
5. A final strict JSON Schema request chooses `internal`, `external`, or
   `unclassified`, one fixed internal category or a normalized external issuer,
   confidence, and evidence source aliases.
6. If rules and model disagree, required fields are absent, or confidence is
   below the threshold, one independent bounded verification request runs.
7. If verification still cannot establish a valid result, the item is
   `unclassified`.

Source aliases such as `S1` are used in prompts instead of durable OA identifiers.
The response is rejected if it invents a source alias, category, document number,
or issuer not supported by the supplied evidence. Prompts and responses are not
written to logs or Git.

Classification is cached by the ordered source SHA256 set, model identity,
rules version, prompt version, and schema version. A changed version creates a
new result; it does not alter originals. Republishing a changed category uses an
atomic target write and removes only the prior OARadar-managed Markdown path
after the new target validates.

## 5. One-time cleanup and rebuild

### 5.1 Preflight

Before changing files:

1. stop the Web, OA worker, Markdown worker, and timers;
2. create a consistent SQLite backup outside `data/`;
3. inventory every candidate original from the database;
4. recheck file existence, regular-file type, size, SHA256, and safe relative
   path;
5. report aggregate ready, missing, mismatched, depth-limit, file-count, and byte
   totals without printing OA names;
6. refuse cleanup if any file marked as an original lacks a safe disposition.

### 5.2 Fast safe rebuild

Because the source and target are on the same filesystem, verified originals are
first hard-linked into a private staging tree. If hard-linking is unavailable,
the implementation uses copy-plus-SHA256 verification. Staging never modifies a
source inode.

The application then:

1. backs up and moves the operational database to the XDG state directory;
2. builds `data_next/originals/` from verified originals;
3. rebuilds `data_next/markdown/` using the bounded pipeline;
4. validates original count and SHA256 parity, Markdown links, indexes,
   classifications, unsupported outcomes, and retryable failures;
5. renames the old `data/` to a uniquely named local legacy directory and
   atomically renames `data_next/` to `data/`;
6. starts services and runs local smoke checks without contacting OA, Feishu, or
   the model;
7. rolls directory names back immediately if smoke checks fail.

After successful validation and smoke checks, the user's 2026-08-23 instruction
authorizes permanent removal of non-original legacy content. Deleting legacy
hard links does not delete the retained original inode. The cleanup command must
resolve exact paths, refuse symlinks and unexpected top-level entries, and never
use an unbounded recursive target.

No historical data is downloaded again from OA for this rebuild.

## 6. Cleanup review notifications

Future quarantine cleanup is never a silent timer:

- the dashboard shows an aggregate `待人工审核` alert when retention matures;
- if Feishu is configured, one aggregate reminder is sent without OA titles,
  paths, or content;
- `oa data review <run-id>` reports categories, counts, bytes, missing files,
  changed files, and recoverability;
- purge requires the exact run ID and confirmation string;
- no scheduled task permanently deletes quarantined files.

The existing cleanup run `1` is eligible for this review flow. Its previously
verified scope contains only rebuildable or temporary content; it contains no
protected Done originals.

## 7. Product and code boundaries

The WebUI keeps only Overview, Pending notifications, Done materials, Markdown
output, and Settings. It gains no manual classification page. Retired audit,
curation, review, policy, backfill, vault, and governance routes are removed from
the production router after any still-required read-only migration helper is
extracted.

The production worker admits only the three flows in section 3. Historical
Markdown work is selected from locally verified originals and cannot be blocked
by an unrelated online-audit campaign.

## 8. Acceptance criteria

1. `data/` has exactly `originals/` and `markdown/` at its top level.
2. Every retained original has a ledger record and matching size and SHA256.
3. Every verified historical original has been retained or has a stable,
   explicit blocking error; no silent loss is allowed.
4. No database, browser profile, log, report, backup, quarantine, parser product,
   old projection, or runtime lock remains in `data/`.
5. Every supported attachment has Markdown or an explicit retryable failure.
6. Every unsupported attachment is listed in its item index.
7. Every item has exactly one `_index.md`.
8. Every item has a valid model/rule classification or is explicitly
   `unclassified`.
9. Every model request obeys the fixed budgets, strict schema, local-only
   endpoint, and single-concurrency rule.
10. Pending notification, deduplication, successful cleanup, Done incremental
    download, restart recovery, and Markdown idempotency tests pass after the
    state move.
11. Container depth beyond 10 produces `depth_limit_reached` and never a false
    complete result.
12. The public-release check, complete synthetic test suite, frontend checks,
    build, and local smoke tests pass without real OA fixtures.

## 9. Out of scope

- modifying, approving, replying to, deleting, or forwarding OA records;
- re-downloading the historical corpus;
- a manual classification UI;
- AI summaries, curated knowledge documents, review queues, knowledge graphs, or
  online Markdown editing;
- retaining parser intermediates for possible future use;
- committing any real OA data, database, profile, log, report, or downloaded
  file.
