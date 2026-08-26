# Feature Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver OA Markdown V1 in three independently gated phases, with classification Dry Run available before any formal Markdown publication.

**Architecture:** Phase 1 creates versioned package classification with metadata-first escalation, private local rules, manual locks, on-demand parsing, local Qwen, and WebUI review. Phase 2 builds immutable Markdown candidates and proves one-to-one reconciliation without changing the formal view. Phase 3 promotes candidates through one atomic `current` pointer and proves crash recovery before exposing publish controls.

**Tech Stack:** Python 3.12, SQLAlchemy 2, Alembic, Pydantic 2, FastAPI, Typer, React 19, TypeScript 5.8, Vite 7, pytest, local Ollama/Qwen.

**Spec:** `docs/superpowers/specs/2026-08-26-oa-markdown-v1-classification-design.md`

**Plan Set:** OA Markdown V1 Delivery Roadmap

## Global Constraints

- OA content is confidential and local-only; never commit real names, OA titles, content, identifiers, reports, databases, logs, browser state, or downloaded files.
- `data/originals/` is immutable and read-only.
- `data/` has exactly two top-level directories: `originals/` and `markdown/`.
- OA identifiers are text; archive paths are relative to `data_root`.
- Container traversal stops at depth 10 and becomes incomplete when unvisited children remain.
- Metadata rules run before parsing; only unresolved items may request parsing; only content-unresolved items may call local Qwen.
- Manual locked decisions are never overwritten automatically.
- Formal output has no `unclassified` or `needs_review` directory.
- Package title segment is at most 120 UTF-8 bytes; the complete package component is at most 160 UTF-8 bytes; OA suffix is 12 lowercase hex characters.
- First historical Dry Run baseline: `8119 total = 1975 excluded + 6144 classification targets`.
- The 40 configured initiator identifiers must map to internal, external, mixed, system, or explicitly reported unknown.

---

## Migration Sequence

### Migration 0038: Classification and versioned parse identity

Create:

- `classification_runs`: immutable full/incremental run identity, versions, target count, summary, and lifecycle.
- `classification_run_items`: frozen target membership and per-phase durable progress.
- `classification_decisions`: versioned item decisions, `decision_input_sha256`, manual lock, dual status axes, package metadata, and one current decision per OA item.
- `classification_evidence`: ordered typed evidence with `package` or `attachment` scope.

Alter:

- `parse_artifacts.profile_version`, defaulting existing rows to `legacy`.
- Unique parse reuse index over content object, engine, engine version, profile version, and config hash.

No current production classification data is overwritten. Existing `oa_items` classification columns remain a denormalized current snapshot.

### Migration 0039: Markdown build ledger

Create:

- `markdown_builds`: frozen classification run, manifest hash, expected counts, actual counts, QA status, and build relative path.
- `markdown_build_items`: exactly one row per publishable OA item, unique package relative path, index hash, and build status.

The migration does not create or switch formal files.

### Migration 0040: Publication release and recovery ledger

Create:

- `publication_releases`: immutable successful release identity, manifest hash, storage location, and active/previous/deleted state.
- `publication_operations`: publish/rollback/delete state machine, old and new targets, checkpoint, actor, error, and recovery status.

Formal publish endpoints remain disabled until the Publication atomicity Gate passes.

## Phase and Gate Order

1. **Phase 1 — Classification Dry Run**
   Plan: `docs/superpowers/plans/2026-08-26-oa-classification-dry-run-implementation.md`
   Exit Gate: all frozen targets have a mutually exclusive durable state; WebUI can review/filter/export; metadata-resolved items did not parse; manual locks survive reruns; no formal Markdown changed.

2. **Phase 2 — Markdown Candidate Build**
   Plan: `docs/superpowers/plans/2026-08-26-oa-markdown-build-implementation.md`
   Entry Gate: Phase 1 Dry Run approved by the user.
   Exit Gate: candidate `_index.md` count equals publishable OA count; every publishable OA key maps to exactly one package; all hashes, links, paths, and frontmatter validate; formal output remains unchanged.

3. **Phase 3 — Publication and Recovery**
   Plan: `docs/superpowers/plans/2026-08-26-oa-publication-implementation.md`
   Entry Gate: a Phase 2 build is approved by the user.
   Exit Gate: every fault-injection checkpoint recovers to a complete old or complete new release; rollback is repeatable; guarded deletion affects derived Markdown only; publish requires explicit WebUI confirmation.

## Program-Level Acceptance

- [ ] `8119 = excluded + publishable + integrity_blocked + needs_review` for the first frozen historical run.
- [ ] `excluded = 1975` and `classification_target = 6144` for that run.
- [ ] All configured initiator identifiers have a reported role; unknown is never silently defaulted.
- [ ] No real private rule value is present in `git grep` output or test fixtures.
- [ ] `find data -mindepth 1 -maxdepth 1 -type d` returns only `data/originals` and `data/markdown`.
- [ ] No phase modifies any file below `data/originals/`.
- [ ] Formal publication remains unavailable until all three phase Gates pass.
