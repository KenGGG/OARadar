# PDF MinerU Reconversion Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Persistently reconvert every verified PDF without a current successful MinerU export and expose independent progress/controls in the audit page.

**Architecture:** Extend the durable Markdown task ledger with an engine override and campaign label. Seed eligible PDF tasks idempotently, prioritize them in the existing single GPU-coordinated Markdown Worker, and publish successful MinerU output atomically over the prior Markdown.

**Tech Stack:** SQLAlchemy/Alembic, FastAPI, Typer worker, React/TypeScript, pytest.

## Tasks

- [ ] Add migration/model fields for `requested_engine`, `campaign`, and independent PDF pause control.
- [ ] Add failing tests for PDF eligibility, idempotent seeding, skip-current-MinerU, and independent pause.
- [ ] Implement campaign service, priority claim, forced MinerU conversion, progress and events.
- [ ] Add start/pause/resume/retry API endpoints and tests.
- [ ] Add audit-page PDF MinerU progress, buttons and logs.
- [ ] Migrate, start campaign, and verify GPU-safe progress plus full regression suite.
