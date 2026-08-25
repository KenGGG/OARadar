# Done Download Convergence Design

## Objective

Restore the full OA Done download campaign and make its progress truthful without
expanding OARadar's production surface. The change is limited to Done manifest
download correctness, collision-safe storage below `data/originals`, and the
minimum overview fields needed to distinguish download work from Markdown work.

`data/` must continue to contain exactly two product roots:

- `data/originals/` for immutable OA originals;
- `data/markdown/` for Markdown output.

The design does not add another data root, enable timers, redesign the Markdown
pipeline, or alter OA records. Every OA interaction remains read-only.

## Current Failure Modes

The current campaign exposes four coupled correctness problems:

1. A long-lived browser closed during a campaign. The command caught the
   resulting `TargetClosedError` as an item error and marked 771 untouched items
   as individual download failures.
2. The durable worker resumes by slicing the immutable target-key list at
   `progress_current`. Progress counts and target-list positions diverge after
   local archive reuse and process recovery, so 15 targets were skipped.
3. A job is marked `completed` when the child command exits zero, even when
   target items remain pending or failed. Its stored `2808/2879` progress can
   therefore coexist with a completed status.
4. Done directories use only initiation date and title. Same-day items with the
   same normalized title share paths. The current data contains 271 collision
   groups and six database file records whose expected hashes no longer match
   the bytes at their shared paths.

The overview adds a presentation problem: it calls all archive-complete items
"queued" when they lack Markdown output even though no durable queue entries
exist. Operators cannot distinguish download backlog from Markdown readiness.

## Chosen Approach

Use the existing manifest, operation-job, file, and archive models. Add no new
pipeline or data root. Correct the retry protocol by deriving progress from
per-target database facts rather than treating an integer as a list cursor.
Treat browser loss as a campaign-level interruption, not an item-level result.
Repair only colliding archive directories; leave non-colliding legacy paths
unchanged.

This is intentionally narrower than a pipeline rewrite. The historical batch,
curation, timer, Pending, and Markdown worker behaviors remain out of scope.

## Durable Retry Semantics

### Immutable campaign start

`OperationJob.started_at` is set only on the first start. Recovery must not
replace it. It is the attempt boundary for the campaign.

For the job's immutable `oa_item_keys`, a target is considered attempted in the
current campaign when either:

- it is already terminal (`downloaded`, `no_attachment`, or `skipped`); or
- `last_retry_at >= job.started_at` and its status records the result of that
  attempt.

A target is still eligible when it is `pending_download` or `download_failed`
and has no attempt at or after the campaign start. Recovery queries these facts
across the full target set. It never slices `oa_item_keys` by
`progress_current`.

### Bounded browser sessions

The worker invokes the existing manifest download command in bounded chunks of
at most 100 eligible targets. Each chunk receives explicit target keys. A fresh
browser session is created for every chunk, limiting the lifetime and blast
radius of a browser process.

After a chunk, the worker recomputes all counts from the database:

- `success`: `downloaded` plus `no_attachment` plus `skipped`;
- `failed`: targets attempted during this campaign whose status is
  `download_failed`, `auth_required`, `depth_limit_reached`, or `partial`;
- `remaining`: eligible targets not attempted during this campaign;
- `progress_current`: `success + failed`;
- `progress_total`: the immutable target count.

The operation finishes `completed` only when `remaining == 0` and `failed == 0`.
It finishes `failed` with a summary error code when every target has been
attempted but one or more targets failed. It must never report completed while
`progress_current < progress_total`.

### Browser-loss handling

The CLI recognizes Playwright target/page/context/browser closure as a systemic
interruption. For the current item it restores the pre-attempt status and
`last_retry_at`, then exits with a dedicated operational error. It does not
continue through later targets and does not increment the item's retry count.

The durable worker may start a fresh chunk up to three consecutive times for a
browser-loss error. It recomputes remaining work before every restart. A
successful chunk resets the consecutive interruption count. After three
consecutive browser losses, the operation finishes failed while every untouched
target remains eligible for a later retry job. This prevents both mass false
failures and an infinite restart loop.

Authentication loss follows the same stop-the-campaign principle, but remains
`auth_required` and requires a later authenticated retry. Ordinary item-specific
capture or attachment errors continue to mark only that item failed.

## Collision-Safe Archive Paths

The human-readable legacy directory remains:

`originals/YYYY/MM/YYYY-MM-DD_<normalized title>`

When more than one OA item resolves to that directory, each item in the
collision group receives a deterministic suffix derived from its confidential
OA key without exposing the key itself:

`originals/YYYY/MM/YYYY-MM-DD_<normalized title>__<10-char SHA-256 prefix>`

The suffix is stable and generated locally. OA identifiers remain text in the
database and are never written verbatim into paths.

New or retried downloads use the legacy path when it is unambiguous and the
suffixed path when a collision exists. Existing non-colliding paths are not
renamed.

## Collision Repair

Add an idempotent local repair operation restricted to duplicate
`OAItem.archive_relpath` groups:

1. Detect colliding item directories from database facts.
2. For every recorded verified file, validate existence, size, and SHA-256
   against the current physical bytes.
3. For a matching file, copy it atomically inside `data/originals` to that
   item's deterministic suffixed directory, then update that item's
   `OAItem.archive_relpath`, `OAManifestItem.archive_relpath`, and
   `ArchivedFile.local_relpath` in one database transaction.
4. If any recorded file for an item does not match, do not copy suspect bytes.
   Mark the manifest `download_failed` at `local_verification` so the normal
   read-only OA downloader reacquires that item into its unique directory.
5. Never delete or overwrite the old shared directory during this repair.

The operation uses only `data/originals`; it does not create staging or report
directories below `data`. Atomic-write staging continues to use the existing
runtime state/cache roots outside `data`.

Running the repair twice produces the same database paths and file contents.

## Overview Contract

Keep the existing overview and navigation. Add or expose these separate Done
counts from manifest status:

- `download_complete_items`: `downloaded + no_attachment`;
- `waiting_download_items`: `discovered + pending_download + processing`;
- `download_failed_items`: `download_failed + auth_required + partial +
  depth_limit_reached`;
- `waiting_markdown_items`: archive-complete items without a successful item
  index;
- `actual_download_queue_items`: active queued/running Done download targets.

The headline must say "等待 Markdown" for the derived Markdown count and must
not call it a queue. "下载队列" is reserved for durable queued/running work.
Existing response fields remain during this change for frontend compatibility,
but the Web UI uses the explicit fields.

No full-disk hash scan runs on each page request. The collision repair resets
known mismatches to `download_failed`; the normal status query then remains
cheap and truthful.

## Recovery Run

After code verification:

1. Stop the OA worker cleanly.
2. Run the collision repair in dry-run mode and verify its aggregate counts.
3. Run the repair for the collision groups. Do not delete legacy directories.
4. Run the full local integrity audit. The expected result before redownload is
   no verified hash mismatches; mismatched items are now explicitly
   `download_failed`.
5. Create one retry job containing every current `pending_download` and
   `download_failed` manifest item. This covers the 15 skipped targets, the 771
   browser-loss failures, and the six collision-corrupted items without
   hard-coding those counts.
6. Restart the OA worker and observe the durable aggregate counts. Do not enable
   the hourly/nightly timers or Markdown worker as part of this change.

The recovery job is allowed to continue after implementation handoff. Success
means it advances with truthful counts and can survive a worker/browser restart;
it does not require waiting for every OA download to finish before the code
change is considered deployed.

## Testing

All tests use synthetic identifiers, titles, paths, and attachment bytes.

Required regressions:

- a resumed job processes eligible keys before and after the old numeric cursor
  boundary;
- terminal targets reconciled locally do not shift or skip unresolved targets;
- `TargetClosedError` restores the current item and aborts the chunk without
  touching later items;
- three consecutive browser-loss interruptions stop without an infinite loop;
- a job with failed targets cannot be completed;
- progress is recomputed from target facts and reaches total only when every
  target has a campaign result;
- same-day same-title items receive distinct deterministic paths while an
  unambiguous legacy item keeps its existing path;
- collision repair copies only hash-valid bytes, resets mismatches for
  redownload, is idempotent, and never deletes the legacy directory;
- the overview distinguishes waiting download, failed download, waiting
  Markdown, and an actual durable queue;
- an integration fixture proves `data/` contains no product root other than
  `originals/` and `markdown/`.

Targeted tests run first, followed by the complete Python test suite and Web UI
build. Production recovery begins only after these checks pass.

## Operational and Security Constraints

- OA access remains read-only: no approve, reply, delete, forward, or record
  mutation.
- Real OA HTML, identifiers, titles, cookies, profiles, databases, downloads,
  and logs are never committed.
- Test fixtures are synthetic or irreversibly redacted.
- Archive paths stored in the database remain relative to `data_root`.
- Container traversal keeps the existing depth-10 rule and continues to enqueue
  `depth_limit_reached` rather than reporting completion.
- The repair performs no deletion. Any later legacy-directory cleanup requires
  a separate explicit design and approval.

## Out of Scope

- Enabling or changing hourly/nightly timers.
- Starting or redesigning the Markdown worker.
- Rebuilding all non-colliding archive paths.
- Changing Pending collection, Feishu delivery, parsing, curation, or review
  workflows.
- Deleting shared legacy directories or adding a third directory below `data/`.
