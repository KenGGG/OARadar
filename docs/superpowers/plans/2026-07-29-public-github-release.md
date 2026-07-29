# Public GitHub Release Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Publish a clean, independently usable public OARadar repository without any real OA endpoint, site identifier, record, credential, runtime artifact, or internal implementation report.

**Architecture:** Keep the application and its runtime material in one working directory but enforce a hard Git boundary: public source and synthetic tests are tracked, while configuration and all OA-derived state live in ignored local paths. A deterministic release scanner validates the exact Git candidate set locally and in CI before any push.

**Tech Stack:** Python 3.12, pytest, uv, TypeScript/React/Vite, npm, Git, GitHub Actions, GitHub CLI.

## Global Constraints

- OA integrations remain read-only; never approve, reply, delete, forward, or alter OA records.
- Never track `data/`, browser profiles, cookies, credentials, Playwright traces, real HTML snapshots, downloads, databases, or runtime logs.
- Tests use only synthetic or irreversibly redacted fixtures.
- OA identifiers remain text and archive paths remain relative to `data_root`.
- Container traversal stops at depth 10; additional children enqueue `depth_limit_reached` and the item is not complete.
- The public repository uses the MIT License.
- Internal reports and real environment configuration remain available locally under ignored paths.

---

### Task 1: Establish the public/private filesystem boundary

**Files:**
- Modify: `.gitignore`
- Create: `private/` (ignored local directory populated by moving internal documents)
- Create: `LICENSE`
- Test: `tests/test_public_release.py`

**Interfaces:**
- Consumes: the approved design in `docs/superpowers/specs/2026-07-29-public-github-release-design.md`.
- Produces: `PUBLIC_DENY_PATHS` expectations used by later release-scanner tests.

- [ ] **Step 1: Write a failing ignore-boundary test**

Add a test that runs `git check-ignore` for `data/example.db`, `private/internal.md`, `config.yaml`, `.env`, `.playwright-cli/page.yml`, `.claude/settings.local.json`, and `runtime/browser-profile/Cookies`; assert every path is ignored. Also assert `config.example.yaml`, `src/oa_knowledge/config.py`, and `tests/fixtures/login_synthetic.html` are not ignored.

- [ ] **Step 2: Run the boundary test and verify RED**

Run: `uv run pytest tests/test_public_release.py::test_public_private_git_boundary -v`

Expected: FAIL because `private/`, `config.yaml`, and personal tool/build artifacts are not all covered.

- [ ] **Step 3: Implement the ignore boundary and local document relocation**

Expand `.gitignore` with explicit local-only paths and generated frontend artifacts. Move all internal stage reports, historical superpowers documents, implementation Goal documents, and real operational notes from `docs/` into `private/docs/`, retaining the approved public design and this plan under `docs/superpowers/`.

- [ ] **Step 4: Add the MIT License and verify GREEN**

Add the standard MIT License with copyright year 2026 and the current GitHub account name determined by `gh api user --jq .login`. Re-run the boundary test and require PASS.

- [ ] **Step 5: Commit the boundary**

Stage only `.gitignore`, `LICENSE`, `tests/test_public_release.py`, the public spec, and this plan. Confirm `private/` and `data/` do not appear in `git status --short`, then commit `chore: establish public repository boundary`.

### Task 2: Make all distributed configuration synthetic and local-first

**Files:**
- Modify: `config.example.yaml`
- Modify: `README.md`
- Test: `tests/test_config.py`
- Test: `tests/test_public_release.py`

**Interfaces:**
- Consumes: existing `load_config(path: Path)` configuration validation.
- Produces: a distributable example that initializes with synthetic OA paths and writes only below `./data`.

- [ ] **Step 1: Write failing example-configuration tests**

Add assertions that `config.example.yaml` contains no IPv4 address, no site-specific long numeric query value, uses `https://oa.example.invalid`, disables LLM by default, keeps `privacy_mode: local_only`, uses `data_root: ./data`, and sets `max_attachment_depth: 10`. Load the example through the real configuration loader.

- [ ] **Step 2: Run tests and verify RED**

Run: `uv run pytest tests/test_public_release.py::test_example_config_is_synthetic tests/test_config.py -v`

Expected: FAIL because the current example contains a real endpoint and site-specific query parameters.

- [ ] **Step 3: Replace real values with explicit synthetic placeholders**

Use the reserved `.invalid` domain and generic `/oa/...` paths without numeric identifiers. Keep browser and storage settings portable, disable remote-capable enrichment by default, and document copying the file to ignored `config.yaml` before editing.

- [ ] **Step 4: Rewrite README for public users**

Remove dates, real record counts, local deployment state, internal experiment results, and organization-specific workflow descriptions. Document installation, configuration, read-only behavior, local storage, model privacy, depth-10 behavior, testing, release scanning, and limitations.

- [ ] **Step 5: Run configuration tests and commit**

Run the focused tests and require PASS. Stage only `config.example.yaml`, `README.md`, and relevant tests; commit `docs: add safe public configuration`.

### Task 3: Build a deterministic tracked-file release scanner

**Files:**
- Create: `scripts/check_public_release.py`
- Modify: `pyproject.toml`
- Test: `tests/test_public_release.py`

**Interfaces:**
- Produces: `scan_paths(paths: Sequence[Path], root: Path) -> list[Finding]`, `candidate_paths(root: Path) -> list[Path]`, and CLI exit status 0 only when there are no findings.
- `Finding` contains `path: str`, `rule: str`, and `line: int | None`; output never prints a matched secret value.

- [ ] **Step 1: Write failing unit tests for forbidden paths and content**

Using temporary synthetic files, test rejection of databases, logs, browser profiles, private directories, non-reserved IP addresses, bearer/API tokens, Cookie headers, personal absolute paths, and site-specific numeric query identifiers. Test acceptance of `example.invalid`, localhost service URLs, synthetic fixtures, and documented test tokens.

- [ ] **Step 2: Run scanner tests and verify RED**

Run: `uv run pytest tests/test_public_release.py -v`

Expected: FAIL because the scanner module does not exist.

- [ ] **Step 3: Implement minimal scanner behavior**

Use only the Python standard library. Obtain candidates from `git ls-files --cached --others --exclude-standard -z`, reject denied paths before reading, skip binary content safely, apply compiled regex rules line by line, and print only `path:line: rule`.

- [ ] **Step 4: Add a narrow explained allowlist**

Allow reserved documentation domains, loopback addresses, and fixed synthetic credential strings only in tests. Keep allowlist entries as rule/path pairs with comments explaining why each is safe.

- [ ] **Step 5: Verify scanner tests and commit**

Run `uv run pytest tests/test_public_release.py -v` and `uv run python scripts/check_public_release.py`; require both to pass before committing `feat: add public release safety scanner`.

### Task 4: Add continuous public-release verification

**Files:**
- Create: `.github/workflows/ci.yml`
- Modify: `webui/package.json` only if a non-mutating check script is absent
- Test: `tests/test_public_release.py`

**Interfaces:**
- Consumes: release scanner CLI, `uv run pytest`, and `npm run build`.
- Produces: CI on pushes and pull requests with no OA credentials or external OA access.

- [ ] **Step 1: Write a failing workflow contract test**

Parse the workflow as text and assert it triggers on push and pull request, installs locked Python dependencies, runs the release scanner and full pytest suite, installs frontend dependencies with `npm ci`, and runs the frontend production build.

- [ ] **Step 2: Run contract test and verify RED**

Run: `uv run pytest tests/test_public_release.py::test_ci_enforces_release_checks -v`

Expected: FAIL because `.github/workflows/ci.yml` does not exist.

- [ ] **Step 3: Add the minimal CI workflow**

Use checkout, Python setup, astral-sh/setup-uv, and Node setup actions pinned to stable major versions. Run no OA login, browser automation, remote model, or data migration against a real configuration.

- [ ] **Step 4: Verify workflow contract and local equivalents**

Run the workflow contract test, full Python suite, `npm ci`, and `npm run build`; require successful exits.

- [ ] **Step 5: Commit CI**

Stage the workflow and any tested script change, then commit `ci: enforce public release checks`.

### Task 5: Audit the complete candidate repository

**Files:**
- Create: `docs/security.md`
- Modify: public documentation or synthetic fixtures only when audit findings require it

**Interfaces:**
- Consumes: Git candidate list and scanner from Task 3.
- Produces: an explicit, reviewed set of tracked public files.

- [ ] **Step 1: Document the public security contract**

Describe local-only data, read-only OA access, prohibited repository content, credential handling, remote-model restrictions, synthetic fixture policy, depth-10 traversal behavior, and responsible disclosure without including real environment details.

- [ ] **Step 2: Generate and inspect the exact candidate list**

Run `git ls-files --cached --others --exclude-standard`, inspect every top-level group, and explicitly stage only approved source, synthetic tests, public docs, manifests, lockfiles, build inputs, workflow, scanner, and license.

- [ ] **Step 3: Scan Git content and object candidates**

Run the scanner against the staged set, inspect `git diff --cached --check`, list staged paths, and use pattern searches on `git show --format= --binary --cached`-equivalent staged blobs without printing matched values. Remove or irreversibly redact every unexplained finding.

- [ ] **Step 4: Verify from a clean temporary clone**

Create a temporary bare/local clone from the repository after committing. Run dependency sync, full Python tests, release scanner, frontend clean install, frontend build, and minimal `oa init` with a copied synthetic config. Confirm all created state stays under the clone's ignored `data/`.

- [ ] **Step 5: Commit the reviewed public application**

After verification, commit the explicit staged set as `release: prepare public OARadar repository`.

### Task 6: Create and verify the public GitHub repository

**Files:**
- No application file changes expected.

**Interfaces:**
- Consumes: clean local `main`, authenticated GitHub CLI, preferred repository name `OARadar` with fallback `oa-radar`.
- Produces: a public GitHub repository whose default branch is `main`.

- [ ] **Step 1: Verify publishing prerequisites**

Run `gh --version`, `gh auth status`, `git status -sb`, full tests, frontend build, and the release scanner. Stop on any failure or unreviewed working-tree change.

- [ ] **Step 2: Resolve the repository name without mutation**

Read the current login with `gh api user --jq .login`. Check `gh repo view LOGIN/OARadar`; if it exists, check `LOGIN/oa-radar`. If both exist, stop and request a new name rather than modifying either repository.

- [ ] **Step 3: Create and push the public repository**

Run `gh repo create NAME --public --source=. --remote=origin --push`. Do not push any other branch or tag.

- [ ] **Step 4: Verify remote state**

Run `gh repo view LOGIN/NAME --json visibility,defaultBranchRef,url`, require `PUBLIC` and `main`, and compare `git ls-tree -r --name-only HEAD` with the remote default-branch tree. Confirm no denied path exists remotely.

- [ ] **Step 5: Report the release**

Provide the repository URL, final commit, test counts, scanner result, frontend build result, and a concise reminder that `config.yaml`, `private/`, and `data/` remain local and untracked.
