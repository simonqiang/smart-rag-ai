# Smart RAG AI Local-First MVP Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver a pilot-ready, local-first SME RAG application with trustworthy citations, current-only knowledge, verified deletion, Ollama/GLM provider choice, and complete responsive support.

**Architecture:** Build a modular Python/FastAPI monolith with a separate worker, React/Vite client, PostgreSQL/pgvector, Redis jobs, local manifest-backed storage, and provider adapters. Deliver vertical slices under TDD; all state-changing knowledge workflows use immutable versions, transactional activation, and a transactional outbox.

**Tech Stack:** Python 3.12+, FastAPI, Pydantic, SQLAlchemy, Alembic, PostgreSQL 16+ with pgvector, Redis, Dramatiq, React, TypeScript, Vite, TanStack Query, Ollama, GLM general API, Docling-led parsers, pytest, Vitest, Testcontainers, Playwright, Docker Compose.

**Spec:** `docs/superpowers/specs/2026-10-04-smart-rag-local-first-design.md`

**Plan level:** This is the master program plan for a multi-subsystem MVP. Tasks are reviewable work packages and schedule units. Before implementing a package that cannot fit one focused session, the executor must split it into 1–2 hour red–green–refactor child tasks in `tasks/todo.md` without changing the package interfaces or acceptance criteria.

## Global Constraints

- Use red–green–refactor TDD for every behavior and bug fix.
- Every first-party module and both app aggregates must reach at least 96% line, statement, function, and branch coverage.
- Changed executable lines require 100% line and branch coverage; designated critical modules require 100% branch coverage.
- Playwright is the required E2E framework and covers desktop, tablet/iPad-class, and mobile, including required portrait/landscape profiles.
- Ollama is the default; GLM is owner-enabled and is never a silent fallback.
- Retrieval always applies workspace, grant, active-version, active-index, and deletion filters at query time.
- Never commit secrets, customer content, generated indexes, or real backups.
- Keep each implementation task within roughly five files; split the task before implementation if the listed boundary grows.
- `make check` is the blocking local/CI quality gate; packaging uses the exact revision that passed it.
- Before every task commit, run its focused tests, affected integration/security/E2E suites, coverage for changed modules, and finally `make check`. A commit step is not authorized until that full gate passes.
- Randomized/property/concurrency tests use a fixed default seed and print the seed on failure; reruns never replace fixing a flaky test.

## Review Focus

- **Crash between source activation and indexing:** Task 18 tests same-transaction outbox persistence and restart catch-up.
- **Restricted content inferred through search/citations:** Tasks 7 and 13 test direct identifiers, autocomplete, ranking, citation preview, and timing-safe denial behavior.
- **Malformed or hostile files/web content:** Tasks 9, 11, 19, and 20 test size/decompression/parser/crawl limits, prompt injection, SSRF, and safe failure.
- **Hosted data transfer surprises:** Task 23 tests exact GLM payloads, consent versions, history limits, redaction, and absence of silent fallback.
- **Responsive workflows hiding destructive controls:** Tasks 15, 17, 22, 24, and 26 run touch/keyboard/viewport cases for every critical action.

---

## File and module map

```text
Makefile                              Stable developer/CI command contract
pyproject.toml                        Python dependencies, lint, typing, pytest, coverage
package.json                          Workspace scripts and frontend toolchain
compose.yaml                          Local application topology
.github/workflows/ci.yml              Blocking quality gates
config/default.yaml                   Non-secret application defaults
apps/api/main.py                      FastAPI composition root
apps/worker/main.py                   Dramatiq worker composition root
apps/web/src/                         Responsive React application
src/foundation/                       Configuration, DB, jobs, storage, outbox, health
src/identity_access/                  Users, sessions, roles, collection grants
src/source_catalog/                   Sources, versions, collections, lifecycle state
src/ai_providers/                     Generation/embedding contracts and adapters
src/ingestion/                        Uploads, parsers, normalization, chunking
src/knowledge_index/                  Embeddings, hybrid index, index generations
src/retrieval_answering/              Authorized retrieval, answers, citations
src/web_sync/                         Crawl safety, snapshots, schedules
src/operations/                       Jobs, audit, diagnostics, backups, analytics
tests/                                Unit, integration, security, evaluation
e2e/                                  Playwright projects, fixtures, page objects, specs
fixtures/                             Synthetic document/site/evaluation fixtures
infra/                                Containers, scripts, release/restore helpers
```

## Phase 0 — Decisions and evaluation baseline

### Task 1: Supported profile and runtime diagnostics

**Files:** Create `config/default.yaml`, `src/foundation/config.py`, `src/foundation/doctor.py`; test `tests/unit/foundation/test_doctor.py`.

**Interfaces:** Produces `Settings.load() -> Settings` and `run_doctor(settings: Settings) -> DoctorReport` for every later task.

- [ ] Write failing tests for supported host checks, writable data paths, missing Docker/Ollama, model-profile reporting, and sanitized output.
- [ ] Run `pytest tests/unit/foundation/test_doctor.py -v`; expect failures for missing interfaces.
- [ ] Implement the typed settings and diagnostic report, pin the selected host/model/limit profile in `config/default.yaml`, and expose no secrets in `DoctorReport`.
- [ ] Run the focused test and `make doctor`; expect all checks to pass on the supported founder machine and actionable failures in fakes.
- [ ] Commit: `feat: define supported runtime profile and diagnostics`.

### Task 2: Versioned RAG evaluation contract

**Files:** Create `fixtures/evaluation/baseline.jsonl`, `src/retrieval_answering/evaluation.py`, `tests/evaluation/test_metrics.py`; modify `Makefile`.

**Interfaces:** Produces `evaluate(cases: Sequence[EvaluationCase], results: Sequence[EvaluationResult]) -> EvaluationReport` with all spec metrics.

- [ ] Write failing metric tests for positive, insufficient-evidence, stale-version, and unauthorized cases, including exact threshold boundary behavior.
- [ ] Run `pytest tests/evaluation/test_metrics.py -v`; expect missing evaluator failure.
- [ ] Implement deterministic metric calculation and a baseline synthetic dataset containing all required case classes.
- [ ] Run `make test-evaluation`; expect a machine-readable report and nonzero exit when a threshold fixture is deliberately below the profile.
- [ ] Commit: `test: add versioned RAG evaluation baseline`.

## Phase 1 — Foundation

### Task 3: Monorepo and blocking quality pipeline

**Files:** Create `pyproject.toml`, `package.json`, `Makefile`, `.github/workflows/ci.yml`, `apps/web/package.json`.

**Interfaces:** Produces `make lint|typecheck|test-unit|test-api|test-web|test-integration|test-security|test-performance|test-e2e|test-evaluation|coverage|build|scan|check`. `make check` executes every preceding gate against a production build and writes a revision-stamped evidence manifest.

- [ ] Add failing smoke tests proving API/worker/web packages are importable/buildable and add a deliberate branch fixture for coverage-threshold verification.
- [ ] Run `make check`; expect failure before toolchain/package configuration exists.
- [ ] Configure Python/TypeScript workspaces, all required Make targets, per-module 96% gates, changed-line 100% gate, production-build Playwright, dependency/container scanning, redacted failure artifacts, and CI jobs.
- [ ] Remove the deliberate uncovered branch only after confirming `make coverage` rejects it; run `make check` and expect PASS.
- [ ] Commit: `chore: scaffold monorepo and quality gates`.

### Task 4: Local Compose topology and health

**Files:** Create `compose.yaml`, `apps/api/main.py`, `apps/worker/main.py`, `infra/postgres/init.sql`; test `tests/integration/test_health.py`.

**Interfaces:** Produces `/health/live`, `/health/ready`, structured correlation/job/workspace/source/version logging, recoverable graceful shutdown, API/worker composition roots, and Compose service names `web`, `api`, `worker`, `postgres`, `redis`.

- [ ] Write failing tests for healthy/unavailable PostgreSQL/Redis, structured redacted logs, graceful API shutdown, and worker lease recovery.
- [ ] Run the focused integration test; expect connection/route failure.
- [ ] Implement containers, pgvector initialization, loopback-only gateway, persistent volumes, structured logging, graceful shutdown, and distinct liveness/readiness behavior.
- [ ] Run `make up && make doctor && pytest tests/integration/test_health.py -v`; expect PASS.
- [ ] Commit: `feat: add local service topology and health checks`.

### Task 5: Persistence, jobs, storage, audit/outbox, and backup-inventory ports

**Files:** Create `src/foundation/unit_of_work.py`, `src/foundation/jobs.py`, `src/foundation/storage.py`, `src/foundation/events.py`; test `tests/integration/foundation/test_primitives.py`.

**Interfaces:** Produces `UnitOfWork.transaction()`, `JobQueue.enqueue(JobCommand)`, `ObjectStore.put/get/delete`, `EventWriter.record(AuditEvent|OutboxEvent, transaction)`, and `ManagedBackupInventory.list_containing(source_id)/purge_containing(source_id)`.

- [ ] Write failing tests for rollback, idempotent enqueue, manifest-addressed storage, transactional audit/outbox atomicity, consumer acknowledgement after success only, and deletion-aware backup inventory/purge port behavior.
- [ ] Run the focused test; expect missing primitive failures.
- [ ] Implement the smallest PostgreSQL/Redis/filesystem adapters and in-memory backup-inventory fake satisfying the contracts; keep Redis non-authoritative and events in the same transaction as state changes.
- [ ] Restart dependencies during integration tests and verify retry/recovery remains deterministic.
- [ ] Commit: `feat: add persistence jobs storage and event foundations`.

## Phase 2 — Identity and source catalog

### Task 6: First-run orchestration, first owner, and secure sessions

**Files:** Create `src/identity_access/setup.py`, `src/identity_access/auth.py`, `apps/api/routes/auth.py`, `apps/web/src/features/auth/SetupPage.tsx`; test `e2e/first-run.spec.ts`.

**Interfaces:** Produces `check_setup_dependencies`, resumable `initialize_workspace`, `create_first_owner`, `authenticate`, `require_session`, `/api/setup|session` contracts, and transactional security audit events through `EventWriter`.

- [ ] Write failing API/Playwright tests for dependency success, unavailable database/Redis/Ollama, unwritable storage, missing model, resume after failure, first-owner exclusivity, Argon2id storage, wrong password, expired/revoked session, and secure cookies.
- [ ] Run the focused test; expect route/service failures.
- [ ] Implement setup orchestration, auth service, transactional security audit events, schema/migration, API routes, and responsive setup/sign-in pages with actionable recovery.
- [ ] Run the complete first-run Playwright matrix on mobile/tablet/desktop plus focused auth integration tests.
- [ ] Commit: `feat: add first owner setup and secure sessions`.

### Task 7: Roles, collections, and grants

**Files:** Create `src/identity_access/authorization.py`, `src/identity_access/grants.py`, `apps/api/routes/users.py`, `apps/web/src/features/settings/UserAccessPage.tsx`; test `tests/security/test_collection_isolation.py`.

**Interfaces:** Produces `AccessContext`, `authorize(action, resource, context)`, and `accessible_collection_ids(user_id, workspace_id)`.

- [ ] Write failing tests for owner/admin/member permissions, cross-workspace IDs, revoked grants, direct object lookup, autocomplete, and timing-safe not-found behavior.
- [ ] Run the security test; expect missing authorization failures.
- [ ] Implement query-scoped grant enforcement, role administration endpoints, responsive user/collection access controls, and transactional access-change audit events through `EventWriter`.
- [ ] Run security, component, and integration tests; require 100% authorization branch coverage.
- [ ] Commit: `feat: enforce workspace roles and collection grants`.

### Task 8: Source catalog and responsive shell

**Files:** Create `src/source_catalog/catalog.py`, `apps/api/routes/sources.py`, `apps/web/src/AppShell.tsx`, `apps/web/src/features/sources/SourceListPage.tsx`; test `e2e/source-catalog.spec.ts`.

**Interfaces:** Produces `SourceCatalog.create/list/get`, source DTOs, and responsive navigation routes.

- [ ] Write failing API/component/Playwright tests for empty state, permitted list, restricted source, loading/error states, and navigation at required viewports.
- [ ] Run focused tests; expect missing catalog/UI failures.
- [ ] Implement source/collection records, filtered endpoints, mobile navigation, tablet layout, and desktop shell.
- [ ] Run API tests and `npx playwright test e2e/source-catalog.spec.ts`; expect PASS without horizontal overflow or hover-only actions.
- [ ] Commit: `feat: add authorized source catalog and app shell`.

## Phase 3 — First end-to-end RAG slice

### Task 9: Safe upload registration and original storage

**Files:** Create `src/ingestion/uploads.py`, `apps/api/routes/uploads.py`, `apps/web/src/features/sources/UploadSource.tsx`; test `tests/integration/ingestion/test_uploads.py`.

**Interfaces:** Produces `register_upload(UploadRequest, AccessContext) -> SourceVersionId`, upload status DTOs, and transactional source-created audit events.

- [ ] Write failing tests for valid file, checksum, duplicate warning, corrupt/encrypted/oversized/unsupported input, interrupted stream, and unauthorized collection.
- [ ] Run the focused test; expect missing upload behavior.
- [ ] Implement streaming validation, manifest storage, version creation with audit event, and responsive upload UI.
- [ ] Run integration/component tests and confirm invalid content never becomes active.
- [ ] Commit: `feat: register and store source uploads safely`.

### Task 10: AI provider contracts and Ollama

**Files:** Create `src/ai_providers/contracts.py`, `src/ai_providers/ollama.py`, `src/ai_providers/fakes.py`; test `tests/unit/ai_providers/test_contracts.py`.

**Interfaces:** Produces `GenerationProvider.generate(GenerationRequest)`, `EmbeddingProvider.embed(list[str])`, typed results/errors, and deterministic test fakes.

- [ ] Write failing contract tests for generation, embeddings, timeout, missing model, dimension mismatch, invalid response, and secret-safe diagnostics.
- [ ] Run focused tests; expect missing adapter failure.
- [ ] Implement provider-neutral types and Ollama HTTP adapters; no provider SDK types cross the boundary.
- [ ] Run contract tests and optional marked live smoke tests; expect deterministic blocking suite PASS without network.
- [ ] Commit: `feat: add AI provider contracts and Ollama adapters`.

### Task 11: PDF, TXT, and Markdown extraction

**Files:** Create `src/ingestion/extraction.py`, `src/ingestion/parsers/basic.py`, `apps/worker/tasks/extract.py`; test `tests/integration/ingestion/test_basic_parsers.py`.

**Interfaces:** Produces `extract(SourceObject) -> ExtractedDocument` with ordered blocks and `SourceLocation`.

- [ ] Write failing fixture tests for text/Markdown/PDF locations, empty/scanned PDF warning, malformed/encrypted file, limits, cancellation, and retry.
- [ ] Run focused tests; expect parser/task failures.
- [ ] Implement normalized block output and idempotent worker state transitions.
- [ ] Run parser and worker tests; assert failed extraction leaves the active pointer unchanged.
- [ ] Commit: `feat: extract initial document formats`.

### Task 12: Chunking, embeddings, and index activation

**Files:** Create `src/ingestion/chunking.py`, `src/knowledge_index/indexer.py`, `src/knowledge_index/generations.py`; test `tests/integration/knowledge_index/test_indexing.py`.

**Interfaces:** Produces `chunk(ExtractedDocument, ChunkPolicy)`, `index_version`, and `activate_generation`.

- [ ] Write failing tests for deterministic chunk boundaries/locations, empty content, embedding batch failure, dimension mismatch, duplicate checksum, and atomic activation.
- [ ] Run focused tests; expect missing indexing failures.
- [ ] Implement chunk policy versioning, pgvector persistence, compatibility keys, staging, validation, and activation pointer.
- [ ] Run unit/integration tests and require 100% activation branch coverage.
- [ ] Commit: `feat: build and activate knowledge index generations`.

### Task 13: Authorized hybrid retrieval

**Files:** Create `src/retrieval_answering/retrieval.py`, `src/retrieval_answering/ranking.py`, `apps/api/routes/search.py`; test `tests/security/test_retrieval_isolation.py`.

**Interfaces:** Produces `retrieve(Query, AccessContext) -> RankedEvidence` with semantic/keyword scores and source locations.

- [ ] Write failing tests for hybrid merge, deduplication, confidence threshold, active-version/index filters, revocation, deleted source, direct ID, empty query, and zero leakage.
- [ ] Run focused security/evaluation tests; expect missing retrieval failure.
- [ ] Implement database-filtered semantic/full-text queries and observable reranking.
- [ ] Run security and evaluation suites; require threshold PASS and 100% critical branch coverage.
- [ ] Commit: `feat: retrieve authorized evidence with hybrid search`.

### Task 14: Grounded answers and validated citations

**Files:** Create `src/retrieval_answering/answering.py`, `src/retrieval_answering/citations.py`, `src/retrieval_answering/conversation.py`, `apps/api/routes/ask.py`; test `tests/unit/retrieval_answering/test_answering.py`.

**Interfaces:** Produces `QueryRewriter.rewrite(Question, ConversationContext)`, `answer(Question, RankedEvidence, AccessContext) -> AnswerResult`, `validate_citations`, bounded four-message `ConversationContext`, and typed `AnswerStreamEvent` (`started|delta|citation|completed|error|cancelled`) with request/resume identifiers.

- [ ] Write failing tests for grounded response, insufficient evidence without model call, four-message context bound, rewrite-before-retrieval inputs, malformed output, invented citation, timeout, cancellation, reconnect/resume, prompt injection, and typed safe error frames.
- [ ] Run focused tests; expect missing answer service failure.
- [ ] Implement conversation retention/deletion semantics, query-rewriter port, evidence-only answer contract, citation IDs limited to supplied evidence, stream/cancellation protocol, response validation, and audit-safe metadata.
- [ ] Run unit/contract/evaluation tests; require citation and groundedness thresholds.
- [ ] Commit: `feat: generate grounded answers with validated citations`.

### Task 15: Responsive Ask workflow

**Files:** Create `packages/contracts/src/ask.ts`, `apps/web/src/features/ask/AskPage.tsx`, `apps/web/src/features/ask/EvidencePanel.tsx`, `apps/web/src/features/ask/useAsk.ts`, `e2e/ask.spec.ts`.

**Interfaces:** Consumes `/api/ask` streaming/result contracts; produces question, filter, answer, citation, insufficient-evidence, feedback hook, and provider badge UI.

- [ ] Write failing contract/component/Playwright cases for successful stream, cancellation, reconnect/error frames, citation open, insufficient evidence, provider timeout, restricted source, empty state, keyboard/touch, and every device project.
- [ ] Run focused Vitest/Playwright commands; expect missing UI failure.
- [ ] Implement the mobile-first Ask flow and adaptive evidence panel with no hover-only interaction.
- [ ] Run the full Ask Playwright matrix; expect no console errors, clipped controls, or horizontal page overflow.
- [ ] Commit: `feat: deliver responsive cited answer experience`.

## Phase 4 — Knowledge lifecycle guarantees

### Task 16: Archive, replacement, and rollback

**Files:** Create `src/source_catalog/version_service.py`, `apps/api/routes/source_versions.py`, `apps/web/src/features/sources/VersionHistory.tsx`; test `tests/integration/source_catalog/test_version_activation.py`.

**Interfaces:** Produces `archive_source`, `unarchive_source`, `stage_replacement`, `activate_ready_version`, and `rollback_as_new_version`; every state change accepts `EventWriter` and records its audit/outbox event in the same transaction.

- [ ] Write failing authorization/concurrency tests for admin archive/unarchive, immediate retrieval exclusion, successful cutover, extraction failure, competing replacement, historical citation authorization, rollback-as-new, transactional audit event, and single-active invariant.
- [ ] Run focused tests; expect missing lifecycle service failure.
- [ ] Implement archive transitions, immutable versions, transactional active pointer/events, and responsive archive/version controls.
- [ ] Run integration/E2E tests; assert no query observes mixed or partially processed content.
- [ ] Commit: `feat: manage immutable source replacements and rollback`.

### Task 17: Permanent deletion and verification

**Files:** Create `src/source_catalog/deletion.py`, `apps/worker/tasks/delete_source.py`, `apps/web/src/features/sources/DeleteSourceDialog.tsx`; test `tests/security/test_source_deletion.py`.

**Interfaces:** Produces `request_permanent_deletion`, idempotent `delete_source`, and `DeletionEvidence`; consumes `ManagedBackupInventory`, conversation/citation repositories, and transactional `EventWriter`.

- [ ] Write failing tests for owner-only request, transactional tombstone/audit event, immediate exclusion, manifest/vector/cache/conversation/backup purge-port invocation, partial failure, retry, redaction, and zero-count evidence.
- [ ] Run focused tests; expect missing deletion behavior.
- [ ] Implement tombstone/audit transaction, manifest-driven purge, backup/conversation ports, verification receipts, and accessible destructive confirmation.
- [ ] Run security/integration/Playwright tests; require 100% deletion branch coverage.
- [ ] Commit: `feat: verify permanent source deletion`.

### Task 18: Concurrent-safe embedding index rebuild

**Files:** Create `src/knowledge_index/rebuild.py`, `apps/worker/tasks/rebuild_index.py`, `apps/api/routes/index_generations.py`; test `tests/integration/knowledge_index/test_rebuild_concurrency.py`.

**Interfaces:** Produces `start_rebuild`, `apply_outbox_event`, `verify_candidate`, and `activate_candidate`; cutover emits its audit event transactionally through `EventWriter`.

- [ ] Write failing tests for activation/deletion during rebuild, crash before/after acknowledgement, duplicate event, watermark catch-up, dimension mismatch, and safe refusal.
- [ ] Run focused tests; expect missing rebuild coordinator failure.
- [ ] Implement candidate generation, same-transaction event consumption contract, idempotent replay, short final lock, exact membership verification, and atomic cutover.
- [ ] Run concurrency tests with a fixed default seed across a deterministic seed matrix; print failing seeds and require 100% critical branch coverage.
- [ ] Commit: `feat: rebuild embedding indexes without missing changes`.

## Phase 5 — Ingestion breadth and website sync

### Task 19: Business document and OCR adapters

**Files:** Create `src/ingestion/parsers/office.py`, `src/ingestion/parsers/spreadsheet.py`, `src/ingestion/parsers/ocr.py`; test `tests/integration/ingestion/test_business_formats.py`.

**Interfaces:** Extends `extract` for DOCX, PPTX, XLSX, CSV, and images while preserving paragraph/slide/sheet/cell/page locations.

- [ ] Write failing fixtures for valid and malformed formats, merged cells, formulas, large sheets, scanned image, OCR failure, archive bomb, and resource limits.
- [ ] Run focused tests; expect unsupported-format failures.
- [ ] Implement adapters behind the existing extraction registry and emit actionable warnings/quality metadata.
- [ ] Run parser/security tests; every supported format must pass location-aware citation assertions.
- [ ] Commit: `feat: support business documents and local OCR`.

### Task 20: Safe website crawling and snapshots

**Files:** Create `src/web_sync/crawl_policy.py`, `src/web_sync/crawler.py`, `src/web_sync/snapshots.py`; test `tests/security/test_crawl_safety.py`.

**Interfaces:** Produces `CrawlPolicy.validate_url`, `crawl(CrawlDefinition) -> CrawlResult`, and `build_snapshot`.

**Dependencies:** Tasks 5, 11–13, 16, and 18. A website snapshot is a source version and must use the same staging, indexing, activation pointer, and transactional outbox as an uploaded replacement.

- [ ] Write failing tests for sitemap/start URL, host/path filters, DNS/private IP, redirect escape, response/depth/page/time limits, duplicate URL, timeout, and partial crawl.
- [ ] Run focused tests; expect missing crawler failure.
- [ ] Implement safe HTTP-first crawling, normalized URLs, meaningful-content extraction, content hashes, and immutable content-addressed snapshot membership that stages through `version_service` and `indexer`.
- [ ] Run security/integration tests against a controlled local site; require zero private-network escape.
- [ ] Commit: `feat: crawl approved websites into immutable snapshots`.

### Task 21: Scheduled synchronization and freshness UI

**Files:** Create `src/web_sync/scheduler.py`, `apps/worker/tasks/sync_site.py`, `apps/web/src/features/sources/WebSyncPanel.tsx`; test `tests/integration/web_sync/test_scheduling.py`.

**Interfaces:** Produces `schedule_sync`, idempotent `run_sync`, freshness DTOs, and manual retry.

**Dependencies:** Tasks 16, 18, and 20; consumes the shared source-version activation and outbox contracts.

- [ ] Write failing clock-controlled and retrieval tests for due/not-due, unchanged reuse, changed page, removed page after complete indexed snapshot, failed/partial crawl retaining old snapshot, atomic activation, outbox emission, and retry.
- [ ] Run focused tests; expect missing scheduler/task failure.
- [ ] Implement schedule persistence, job orchestration, shared index validation/atomic activation, and responsive health/status controls.
- [ ] Run crawl-to-retrieval integration plus Playwright source-health scenarios on all device projects; prove removed pages disappear only after successful cutover.
- [ ] Commit: `feat: schedule website synchronization and freshness`.

## Phase 6 — Operations and provider choice

### Task 22: Activity, retry, health, and diagnostics

**Files:** Create `src/operations/job_queries.py`, `src/operations/audit.py`, `apps/api/routes/operations.py`, `apps/web/src/features/activity/ActivityPage.tsx`; test `e2e/activity.spec.ts`.

**Interfaces:** Produces paginated job/source-health/audit DTOs, `retry_job`, and redacted diagnostic export by querying the immutable events written through Task 5.

- [ ] Write failing tests for all job states, safe/unsafe retry, required audit events, role access, redaction, empty state, storage warning, provider outage, and responsive operation.
- [ ] Run focused API/UI/E2E tests; expect missing operations behavior.
- [ ] Implement audit/job read models, retry rules, health aggregation, and adaptive activity/diagnostics UI; do not introduce a second audit-writing path.
- [ ] Run tests on all Playwright projects; inspect failure artifacts for secret/source redaction.
- [ ] Commit: `feat: expose safe operational diagnostics and retry`.

### Task 23: GLM generation and explicit consent

**Files:** Create `src/ai_providers/glm.py`, `src/ai_providers/provider_settings.py`, `apps/api/routes/provider_settings.py`, `apps/web/src/features/settings/AIProviderSettings.tsx`; test `tests/security/test_hosted_provider_privacy.py`.

**Interfaces:** Implements `GenerationProvider` and `QueryRewriter`; produces secret-reference-backed `test_provider`, `activate_generation_provider(config, consent_version)`, settings DTOs, and transactional provider-change audit events.

- [ ] Write failing tests for exact generation/rewrite payloads, optional four-message limit, excluded fields, invalid key/model, outage, consent mismatch, switch without re-index, and no fallback.
- [ ] Run focused tests; expect missing GLM/settings failure.
- [ ] Implement the general API generation/rewriter adapter, settings API, encrypted/local secret reference, versioned consent, four-message limit, test-before-activate, transactional audit event, and visible provider state.
- [ ] Run contract/security/Playwright tests with deterministic fake GLM; keep live smoke marked optional.
- [ ] Commit: `feat: add consented GLM generation provider`.

### Task 24: Encrypted backup and clean restore

**Files:** Create `src/operations/backup.py`, `infra/scripts/backup_restore.py`, `apps/web/src/features/settings/BackupRestore.tsx`; test `tests/integration/operations/test_backup_restore.py`.

**Interfaces:** Implements Task 5 `ManagedBackupInventory`; produces `create_backup`, `inspect_backup`, `restore_backup`, and transactional backup/restore audit events with signed/checksummed manifest and external recovery key.

- [ ] Write failing clean-install tests for success, wrong key, corruption, incompatible schema, missing object, inventory lookup, live source-deletion purge integration, retention purge, unauthorized actor, and transactional audit events.
- [ ] Run focused tests; expect missing backup service failure.
- [ ] Implement the backup inventory/purge port, authenticated encryption, manifest/checksums, owner-only job/API orchestration, clean-target enforcement, migrations, audit events, and retrieval smoke verification.
- [ ] Run full backup/restore integration plus responsive Playwright flow.
- [ ] Commit: `feat: add encrypted verified backup and restore`.

### Task 25: Feedback and unanswered-question analytics

**Files:** Create `src/operations/feedback.py`, `apps/api/routes/analytics.py`, `apps/web/src/features/analytics/AnalyticsPage.tsx`; test `tests/integration/operations/test_feedback.py`.

**Interfaces:** Produces `record_feedback`, privacy-safe aggregates, and role-filtered analytics DTOs.

- [ ] Write failing tests for helpful/unhelpful, unanswered classification, duplicate input, role denial, deleted/redacted sources, no raw-content telemetry, and empty analytics.
- [ ] Run focused tests; expect missing feedback/analytics failure.
- [ ] Implement records/aggregates and responsive analytics screens without external telemetry.
- [ ] Run API/component/Playwright tests on all device projects.
- [ ] Commit: `feat: add privacy-safe answer feedback analytics`.

## Phase 7 — Responsive, security, and performance hardening

### Task 26: Responsive and accessibility completion

**Files:** Modify `apps/web/src/styles/tokens.css`, `apps/web/src/AppShell.tsx`, `e2e/playwright.config.ts`; create `e2e/responsive-accessibility.spec.ts`.

**Interfaces:** Produces shared breakpoints/touch targets and Playwright projects for 320, 768, 1024, 1440 widths plus mobile/tablet orientations.

- [ ] Write failing cross-route tests for every role/action, keyboard/focus, touch targets, no hover-only actions, no page overflow, accessible names, status announcements, and WCAG AA scans.
- [ ] Run responsive Playwright matrix; record current failures.
- [ ] Fix shared layout/components rather than route-specific hacks; preserve all workflows and data in orientation changes.
- [ ] Run `make test-e2e`; expect all projects PASS with zero accessibility violations/console errors.
- [ ] Commit: `fix: complete responsive and accessible workflows`.

### Task 27: Security, performance, and RAG hardening

**Files:** Create `tests/security/test_mvp_threat_model.py`, `tests/performance/test_mvp_targets.py`; modify `fixtures/evaluation/baseline.jsonl`, `src/retrieval_answering/ranking.py`.

**Interfaces:** Consumes the full product; produces blocking security/performance/evaluation reports for the supported profile.

- [ ] Add failing tests for every threat-table control, concurrent office load, 100k-chunk target, provider latency separation, and all blocking RAG metrics.
- [ ] Run `make test-security && make test-performance && make test-evaluation`; record failing controls/metrics without lowering thresholds.
- [ ] Apply the smallest retrieval/config/index improvements that meet targets; do not add unvalidated architecture.
- [ ] Run `make coverage && make test-security && make test-performance && make test-evaluation`; expect all gates PASS, fixed/reported seeds, and zero stale/unauthorized leakage.
- [ ] Commit: `test: harden security performance and RAG quality`.

## Phase 8 — Packaging and pilot release

### Task 28: Pilot packaging and release verification

**Files:** Create `infra/scripts/install.py`, `infra/scripts/upgrade.py`, `docs/operator-guide.md`, `docs/release-checklist.md`; modify `Makefile`.

**Interfaces:** Produces `make setup|up|down|doctor|backup|check|package`, versioned package/evidence manifests, support bundle, safe upgrade, unsafe-downgrade refusal, graceful shutdown, LAN TLS/proxy profile, and rollback procedure.

- [ ] Write failing package/clean-machine tests for first install, restart, safe upgrade, unsafe downgrade, failed upgrade rollback, data preservation, graceful shutdown recovery, LAN host/origin/cookie/proxy policy, diagnostics redaction, and uninstall-without-data-loss default.
- [ ] Run the packaging verification; expect missing scripts/docs failure.
- [ ] Implement repeatable package/setup/upgrade/support-bundle/LAN workflows, measured model/hardware profiles, known limitations, and operator instructions.
- [ ] On a clean supported machine, run `make setup`, immutable `make check`, production-build Playwright, scans, backup/restore, upgrade/rollback, and a design-partner usability rehearsal; retain redacted revision-stamped evidence for the exact packaged commit.
- [ ] Commit: `chore: package and verify pilot release`.

## Plan self-review result

- Every canonical functional area maps to Tasks 1–28.
- Every task has a failing-test step, expected failure run, minimal implementation step, passing verification, and atomic commit.
- Shared interfaces are introduced before consumers.
- The five Review Focus risks have explicit owning tests.
- Tasks that exceed five files during execution must be split before code is written; the task list is a boundary, not permission for oversized changes.
- No implementation begins until the user reviews this plan and selects an execution method.
