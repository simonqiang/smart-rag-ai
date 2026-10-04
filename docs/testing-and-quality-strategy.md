# Testing and Quality Strategy

## 1. Policy

All application behavior is developed with test-driven development (TDD). Every deployable revision must pass automated unit, integration, security, evaluation, and end-to-end tests. Browser E2E tests use Playwright.

Tests cover both:

- **happy paths:** valid inputs and successful user outcomes; and
- **unhappy paths:** invalid input, denied access, missing dependencies, timeouts, partial failures, retries, stale state, and recovery.

No package, installer, release image, or deployment may be produced from a revision that fails a required quality gate.

## 2. TDD workflow

Every new behavior and bug fix follows red–green–refactor:

1. **Red:** write a focused test expressing the required behavior and verify that it fails for the expected reason.
2. **Green:** implement the smallest correct change that makes the test pass.
3. **Refactor:** improve the design while keeping the focused and related tests green.
4. Run the affected unit and integration suites.
5. Run the complete quality gate before merging or packaging.

A bug fix begins with a test that reproduces the bug. Tests are not written merely after implementation to increase the coverage number. Pull requests must identify the tests that drove the change.

## 3. Test levels

| Level | Purpose | Examples |
|---|---|---|
| Unit | Fast verification of isolated domain behavior | State transitions, chunking, score merging, citation validation, URL policy |
| Component | Frontend behavior without a full deployed stack | Forms, validation, loading/error states, permission-dependent controls |
| Integration | Boundaries using real local dependencies where practical | PostgreSQL transactions, `pgvector`, Redis jobs, filesystem, parser fixtures |
| Security | Abuse and isolation behavior | Cross-workspace access, prompt injection, SSRF, secrets, malicious files |
| Evaluation | RAG quality against a versioned truth set | Retrieval recall, groundedness, citations, insufficient-evidence behavior |
| E2E | Critical user journeys through the real browser and application | Setup, ingest, ask, replace, permission change, delete, provider switch, restore |

Most tests should be deterministic unit tests. Integration and E2E tests provide boundary confidence without replacing focused unit coverage.

## 4. Coverage gate

Coverage is measured on first-party executable code. Backend and frontend are reported separately, and the thresholds apply to every first-party package/module as well as to each application's aggregate. A large well-tested module therefore cannot hide an untested module.

Required thresholds for every backend/frontend package or domain module and for both application aggregates:

- line coverage: **at least 96%**;
- statement coverage: **at least 96%**;
- function coverage: **at least 96%**; and
- branch coverage: **at least 96%**.

The target is 100% when tests remain meaningful. The following security- and correctness-critical modules require 100% branch coverage:

- authorization and collection filtering;
- source/version activation and rollback;
- deletion and deletion verification;
- index-generation cutover and change-ledger handling;
- provider privacy/failover policy;
- citation validation; and
- crawl allowlist and private-network blocking.

Generated contracts, third-party/vendor code, declarative migration files, and tool configuration may be excluded. Every exclusion must be explicit in coverage configuration and justified in review. Business logic may not be moved into excluded files to satisfy the threshold.

Coverage is a minimum gate, not proof of correctness. Tests must assert externally visible outcomes and important invariants rather than execute lines without meaningful assertions.

Changed executable lines also require 100% line and branch coverage in the pull request. A justified unreachable branch may be excluded only through an explicit reviewed pragma linked to the reason.

## 5. Playwright E2E policy

Playwright is the only MVP browser E2E framework. Tests run against a production build of the web application and API with isolated PostgreSQL, Redis, and file storage. AI and external website boundaries use deterministic local fakes for the blocking suite; separately marked live smoke tests may exercise Ollama or GLM when credentials and models are available.

The complete (nightly and release-blocking) suite runs the role-based workflow set across Playwright projects representing mobile, tablet/iPad-class, and desktop devices. It verifies at least 320 px, 768 px, 1024 px, and 1440 px viewport widths, including portrait and landscape mobile/tablet orientations. Tests fail on clipped required controls, unintended page-level horizontal scrolling, inaccessible touch targets, or actions available only through hover.

The suite runs at two cadences so after-hours development stays fast without weakening the release bar:

| Gate | When | Playwright scope |
|---|---|---|
| `make check` (PR blocking smoke suite) | Every pull request and local pre-commit | Smoke projects (mobile portrait, tablet landscape, desktop): every critical happy path plus unhappy paths touched by the change |
| Nightly | Every night on `main` | Complete matrix: all widths and orientations, all happy and unhappy paths, accessibility scans |
| `make check-release` | Before any package or deployment | `make check` plus the complete matrix on the same revision |

A nightly failure is a blocking defect: no further feature merges until it is fixed or reverted. Only a revision that passed `make check-release` may be packaged.

Each critical workflow has at least one happy path and relevant unhappy paths:

| Workflow | Happy path | Required unhappy paths |
|---|---|---|
| First-run setup | Owner reaches first cited answer | Database unavailable, storage unwritable, Ollama unavailable/model missing |
| Sign-in/access | Authorized member sees permitted collections; invited member accepts invitation | Wrong password, expired session, expired/reused invitation, restricted collection/source, repeated-failure throttling |
| Document upload | Supported file becomes active/searchable | Unsupported, corrupt, encrypted, oversized, parser/OCR failure |
| Document replacement | New version activates and old version disappears from retrieval | Processing failure preserves old version; concurrent update conflict |
| Website sync | Changed snapshot activates | Blocked host/private IP, timeout, redirect escape, partial crawl keeps prior snapshot |
| Ask | Grounded answer opens correct citation in English, Chinese, and Malay | Insufficient evidence, provider timeout, invalid model response, citation mismatch |
| Provider switch | Ollama changes to GLM without re-indexing | Invalid key/model, rejected consent, GLM unavailable, no silent hosted fallback |
| Permissions | Grant enables access and revocation removes it | Direct URL/API/retrieval attempts remain denied |
| Deletion | Owner deletes and verification reaches zero | Non-owner attempt, partial object failure, retry resumes safely, affected answer redaction |
| Backup/restore | Clean installation restores and retrieves | Wrong key, corrupt archive, incompatible version, deleted-source backup rejection |

Playwright assertions cover browser-visible state, API outcomes, accessibility-critical roles/names, and the absence of console errors. Tests use role- or label-based locators and must not depend on arbitrary sleeps. Traces, screenshots, video, console output, and network logs are retained on failure with secrets and source content redacted.

## 6. Test data and isolation

- Each test owns its users, workspace, sources, jobs, and storage prefix.
- Tests can run in any order and in parallel where supported.
- Time, UUIDs, model responses, and network failures are controllable.
- Parser fixtures are synthetic or approved for repository use; no customer files enter source control.
- Test databases and storage are disposable and never reuse production credentials.
- External APIs are not required for the blocking test suite.

Flaky tests are defects. A failing test is fixed or the change is blocked; repeatedly rerunning it until green is not an acceptable deployment practice. Temporary quarantine requires an owner, issue, expiry date, and an equivalent blocking risk control.

## 7. Quality-gate sequence

```text
format/lint
→ static type checks
→ unit/component tests with coverage
→ integration/security tests with coverage
→ production build
→ Playwright E2E smoke projects (make check) or complete matrix (make check-release)
→ RAG evaluation thresholds
→ dependency licence, dependency, and container security scans
→ package or deploy (make check-release revisions only)
```

All gates run locally through `make check` and in CI on every pull request and main-branch change. The complete Playwright matrix runs nightly and in `make check-release`. Release packaging depends on the same immutable revision that passed `make check-release`. A deployment must provide a documented rollback to the last verified revision.

CI runs the Linux suite on every change and runs `make doctor` plus install smoke tests on Windows 11, macOS (Apple Silicon), and Ubuntu runners nightly and before release.

The initial blocking RAG evaluation profile is measured on the versioned Phase 0 truth set:

- retrieval recall@10: at least 90%;
- mean reciprocal rank@10: at least 75%;
- citation correctness: at least 98%;
- grounded-answer acceptance: at least 95%;
- insufficient-evidence precision: at least 90%;
- insufficient-evidence recall: at least 85%; and
- stale or unauthorized retrieval leakage: exactly zero.

Phase 0 must create enough positive, negative, ambiguous, stale-version, and permission-denied examples to calculate every metric before Phase 1 release packaging. Every case class includes English, Simplified Chinese (`zh-Hans`), Traditional Chinese (`zh-Hant`), Malay, and mixed-language examples, and each of these strata must meet the thresholds independently; an aggregate Chinese score may not hide a failing script. Thresholds may only increase after approval; a temporary reduction blocks production packaging and requires a documented product decision.

How each metric is measured:

| Metric | Measurement | Cadence |
|---|---|---|
| Recall@10, MRR@10, leakage, insufficient-evidence precision/recall | Computed by code against labelled expected passages | Every `make test-evaluation` |
| Citation correctness | Computed by code: cited IDs belong to supplied evidence and the cited span supports the quoted claim | Every `make test-evaluation` |
| Grounded-answer acceptance | Recorded provider responses are replayed through the current end-to-end answer pipeline (retrieval, prompt, citation validation) and compared with human-labelled judgments; an optional local LLM judge is advisory only. Recorded sets are bound to a digest of the prompt, answer pipeline, and model profile, so a relevant change invalidates them and requires re-recording and re-labelling | Every `make test-evaluation` |
| Live-model quality and latency | Fresh answers from a real Ollama/GLM profile with recorded model digest, seed, and parameters | Release gate per model profile |

Synthetic fixtures form the initial truth set. Questions sourced from design partners (with permission, and stored outside the repository if confidential) are added as the customer-discovery track produces them.

## 8. Required commands

```bash
make test-unit          # Aggregate target: runs test-api and test-web
make test-api           # Backend unit/component suite
make test-web           # Frontend unit/component suite
make test-integration   # Database, queue, storage, parser, and API boundaries
make test-security      # Isolation and abuse cases
make test-e2e           # Playwright smoke projects against the composed production build
make test-e2e-full      # Complete Playwright device matrix (nightly and release)
make test-evaluation    # Versioned RAG quality dataset
make coverage           # Enforce all 96% and critical-module 100% thresholds
make check              # Pull-request quality gate
make check-release      # make check plus complete E2E matrix; required for packaging
```

Focused test commands must also exist so the red–green–refactor loop remains fast.

## 9. Release evidence

For each release, retain:

- commit identifier;
- test and coverage summaries for backend and frontend;
- Playwright report;
- RAG evaluation report;
- dependency/container scan results;
- supported hardware/model profile used for verification;
- clean-machine install results for Windows 11, macOS (Apple Silicon), and Ubuntu LTS;
- per-language evaluation results (English, Chinese, Malay); and
- rollback target and instructions.

A manual test may supplement this evidence but never replace a blocking automated gate.
