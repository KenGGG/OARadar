# OARadar Data Rebuild Implementation Index

This index implements `docs/superpowers/specs/2026-08-21-oaradar-data-cleanup-markdown-rebuild-design.md` in four independently reviewable phases.

Execute strictly in order:

1. `2026-08-21-oaradar-data-rebuild-phase-1-classification.md`
2. `2026-08-21-oaradar-data-rebuild-phase-2-clean-archive.md`
3. `2026-08-21-oaradar-data-rebuild-phase-3-markdown.md`
4. `2026-08-21-oaradar-data-rebuild-phase-4-cutover.md`

Phase boundaries are release gates. Do not start the next phase until the current phase's focused tests, `uv run pytest`, `uv run python scripts/check_public_release.py`, `npm run check`, and `npm run build` pass.

No phase may delete or overwrite the current `data/`. Phase 4 may rename directories only after the user explicitly authorizes the cutover. Permanent deletion of `data_legacy_<date>/` is not part of these plans and always requires a later, separate authorization.

