# Agnes Safe Semantic V2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Improve the frozen OA classification snapshot with a two-channel semantic classifier, then build and verify a non-published Candidate V2.

**Architecture:** A local deterministic eligibility gate decides, before any model request, whether an OA is a clearly public external document. Only that narrow set is sent to the approved Agnes OpenAI-compatible endpoint; all other OA content remains local and uses the existing Qwen path. Both channels consume a shared OA-package context built from existing ParseArtifacts and record immutable, versioned classification evidence. The existing classified-only candidate builder then freezes its own decision snapshot and creates Candidate V2 without invoking classification.

**Tech Stack:** Python 3.12, SQLAlchemy/SQLite/Alembic, Pydantic, httpx, existing ParseCacheService/FormatRouter, pytest, Ruff.

**Spec:** user-approved design input (not stored in this repository)

## Global Constraints

- OA originals are confidential and read-only; never commit `data/`, private rules, DBs, or logs.
- Agnes may receive only locally determined `external_public` OA context; uncertainty defaults to `local_only`.
- Do not send paths, user account data, raw original files, or API credentials to Agnes.
- Reuse ParseArtifacts; call FormatRouter only if usable text is missing and semantic classification genuinely needs it.
- Ollama/Qwen remains the local-only semantic fallback and is disabled for the public-Anges path.
- Decisions remain versioned and historical decisions are never overwritten.
- Candidate V2 uses classified-only frozen `(oa_item_key, decision_id)` input, does not classify, and never switches `data/markdown/current`.

---

### Task 1: Local public-data eligibility and provider configuration

**Files:**
- Create: `src/oa_knowledge/classification/agnes_eligibility.py`
- Modify: `src/oa_knowledge/config.py`, `config.yaml`
- Test: `tests/test_agnes_eligibility.py`, `tests/test_provider_settings.py`

**Interfaces:**
- Produces `AgnesEligibility(status: Literal["allowed", "local_only"], reason: str)`.
- Produces `AgnesConfig` with an explicit public endpoint, model, credential environment-variable name, and bounded request controls.

- [ ] Write failing tests proving only unambiguous external public-document signals are allowed and all internal, mixed, unknown, sensitive, or ambiguous signals remain local.
- [ ] Run `uv run pytest tests/test_agnes_eligibility.py tests/test_provider_settings.py -q` and confirm the tests fail because the gate/configuration do not exist.
- [ ] Implement the minimal gate using existing current decision, deterministic evidence, document number, workflow, and attachment-name metadata; do not inspect model output to decide eligibility.
- [ ] Implement explicit configuration validation: only the documented Agnes HTTPS host is accepted for the public provider, credentials are environment-only, and defaults preserve local-only behavior if disabled.
- [ ] Re-run the focused tests and commit the green change.

### Task 2: Audited two-channel semantic classifier

**Files:**
- Create: `src/oa_knowledge/classification/semantic_classifier.py`
- Modify: `src/oa_knowledge/enrich/llm_client.py`, `src/oa_knowledge/db/models.py`, `src/oa_knowledge/db/migrations/versions/0041_agnes_semantic_classification.py`
- Test: `tests/test_semantic_classifier.py`, `tests/test_classification_migration.py`

**Interfaces:**
- Consumes an OA key, frozen current decision, attachment ParseArtifacts, and `AgnesEligibility`.
- Produces schema-validated `SemanticOutcome` and an auditable cache record keyed by OA, ParseArtifact hashes, prompt version, and model.
- Records `decision_source="agnes"` only for accepted Agnes decisions; local model results remain `local_qwen`.

- [ ] Write failing tests for minimal outbound payload, strict schema rejection, cache reuse, eligibility audit evidence, and local routing without an Agnes request.
- [ ] Run the focused tests and confirm expected feature failures.
- [ ] Add the smallest migration needed to permit `agnes` decision source and durable semantic cache/audit records without changing OA Package or multi-issuer schema.
- [ ] Implement the OA-package context builder: attachment boundaries, deterministic long-text selection, no local paths/account data, and reusable ParseArtifacts.
- [ ] Implement bounded retry/backoff for public Agnes only, then fall through to durable `needs_review` on terminal failure rather than leaking content or mutating an invalid decision.
- [ ] Re-run focused tests, migration tests, and commit the green change.

### Task 3: Resumable 6,144-item semantic review run

**Files:**
- Create: `src/oa_knowledge/classification/semantic_run.py`
- Modify: `src/oa_knowledge/cli.py`, `src/oa_knowledge/classification/reporting.py`
- Test: `tests/test_semantic_run.py`, `tests/test_cli.py`

**Interfaces:**
- Creates a scoped, persistent run for exactly the 6,144 non-excluded OA keys.
- Applies needs-review outcomes only at confidence >= 0.90; replaces classified outcomes only at >= 0.95 with body evidence; preserves manual locks.
- Produces durable resume/checkpoint statistics and reports all calls, cache hits, retries, eligibility, and decision diffs.

- [ ] Write failing tests covering frozen scope, excluded=0, threshold behavior, manual-lock preservation, resume after a failed item, and no reparse when usable ParseArtifact exists.
- [ ] Run focused tests and confirm expected failures.
- [ ] Implement durable processing with bounded batches; local-only work invokes existing local model path, public work invokes Agnes after eligibility, and all outputs write evidence before an adopted decision changes.
- [ ] Add the CLI command that starts/resumes this run against the verified active state DB; no Markdown build command is called here.
- [ ] Re-run focused tests and commit the green change.

### Task 4: Classification execution, V2 candidate build, and QA reports

**Files:**
- Modify: `src/oa_knowledge/classified_candidate_build.py`, `src/oa_knowledge/classification/reporting.py`
- Create: `src/oa_knowledge/classification/semantic_qa.py`
- Test: `tests/test_classified_candidate_build.py`, `tests/test_semantic_qa.py`

**Interfaces:**
- Consumes only the effective classified snapshot after Task 3.
- Produces `data/markdown/.builds/full-classified-candidate-v2-<date>/` plus V1→V2 diff, remaining-review, semantic QA, candidate QA, and final run reports.

- [ ] Write failing tests for V1 preservation, V2 snapshot isolation, candidate decision immutability, and report coverage.
- [ ] Run focused tests and confirm expected failures.
- [ ] Implement report generation and QA sampling using synthetic/redacted fixtures only; retain existing V1 package builder behavior and do not publish.
- [ ] Execute the semantic run using the real active state DB with recovery checkpoints, then freeze V2 and build only publishable current decisions.
- [ ] Run all required candidate QA, originals integrity verification, full pytest, and scoped Ruff; commit only source/tests/docs, never runtime data.

## Coverage Review

- Public egress is locally gated and audited: Tasks 1–3.
- Existing ParseArtifacts, local fallback, retries, cache, versioned decisions, thresholds, and manual locks: Tasks 2–3.
- All 6,144 non-excluded OA receive semantic handling; excluded OA are not reclassified: Task 3.
- V1 preservation, V2 frozen candidate, package QA, decision integrity, and no publication: Task 4.
