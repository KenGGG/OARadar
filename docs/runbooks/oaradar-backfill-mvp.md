# OARadar Backfill MVP Runbook

## Safety boundary

This command reads protected originals and writes only a new candidate under:

```text
data/markdown/.builds/<run_id>/
```

It never publishes `current`, never writes an OA record, and never modifies, moves, or deletes `data/originals/`. Do not use the legacy Markdown rebuild command for this campaign.

## Preflight

1. Stop writers to the SQLite database.
2. Confirm aggregate manifest counts and available disk space.
3. Inventory `data/originals/` with relative path, size, and SHA-256 for the before/after comparison.
4. Back up the configured SQLite database below the private state root using SQLite's backup API; run `PRAGMA integrity_check` on the backup.
5. Upgrade the working database to the branch head.
6. Set `OA_CLASSIFICATION_PRIVATE_DIR` to an absolute `0700` directory containing the four required `0600` YAML files. Unknown initiators must be declared `unknown`, not guessed.

## First real run

```bash
uv run oa backfill-mvp \
  --config /data/Projects/OARadar/config.yaml \
  --run-id backfill-mvp-100-20260827 \
  --sample-size 100
```

The deterministic sample is ordinary-heavy and includes available transfer, document-number, mixed-initiator, multi-attachment, no-attachment, template, and abnormal examples. There is no confirmation pause after selection.

## Outputs

- `sample.csv`: selected OA keys and short selection reasons.
- `classification.csv`: metadata decision, confidence, integrity, and review status.
- `exceptions.csv`: per-file conversion/integrity failures without attachment body text.
- `build_manifest.json`: baseline and run counts, reconciliation equations, exception summary, and SHA-256 for every candidate payload other than the self-describing manifest.
- `packages/`: one Package and `_index.md` per selected OA; successful attachment conversions sit beside the index.

`needs_review` is allowed only inside this candidate. It is not a formal publication directory.

## Continuation and verification

Individual file failures do not stop the batch. An unsafe path, incompatible existing run ID, or failed reconciliation does stop it.

Re-running the exact command verifies the existing input hash and every recorded candidate file hash, then returns the same counts without rebuilding. A changed input requires a new run ID.

After the run:

1. Confirm `selected = packages = classified + needs_review`.
2. Confirm `attachments_attempted = converted + failed + skipped`.
3. Count `_index.md` files independently.
4. Recompute all hashes in `build_manifest.json`.
5. Compare the originals inventory with the preflight inventory.
6. Report processed OA, Packages, converted attachments, automatic classifications, reviews, failures/exceptions, and reconciliation—not code-task counts.

## Full target run

Only after the 100-item candidate is reviewed:

```bash
uv run oa backfill-mvp \
  --config /data/Projects/OARadar/config.yaml \
  --run-id backfill-mvp-all-20260827 \
  --all-targets
```

Formal publication, long-term version retention, rollback, Qwen fallback, and WebUI review remain outside this MVP.
