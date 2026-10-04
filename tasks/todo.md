# Smart RAG AI MVP Task List

Each task follows TDD, includes happy and unhappy paths, and must keep all repository quality gates green. Detailed steps and interfaces are in [the execution plan](../docs/superpowers/plans/2026-10-05-smart-rag-mvp-implementation.md).

These are master work packages. Before implementation, any package larger than one focused session must be expanded here into 1–2 hour child tasks while preserving its acceptance criteria, dependencies, and interface contract.

## Phase 0 — Decisions and evaluation

- [ ] **Task 1: Pin the supported development profile and limits**
  - Acceptance: host OS/architecture, dependency versions, model candidates, and initial ingestion limits are recorded and machine-checkable.
  - Verify: `make doctor` reports the profile and fails clearly for missing requirements.
  - Dependencies: none.
- [ ] **Task 2: Create the RAG evaluation dataset and scorer contract**
  - Acceptance: positive, negative, ambiguous, stale, and unauthorized fixtures calculate every blocking metric.
  - Verify: `make test-evaluation` produces a deterministic baseline report.
  - Dependencies: Task 1.

## Phase 1 — Foundation

- [ ] **Task 3: Scaffold the monorepo and blocking quality pipeline**
  - Acceptance: web/API/worker packages build; every required lint/type/unit/integration/security/performance/E2E/evaluation/coverage/build/scan target runs through `make check` in CI.
  - Verify: `make check` passes; an intentional uncovered branch makes `make coverage` fail.
  - Dependencies: Task 1.
- [ ] **Task 4: Start and diagnose the local Compose stack**
  - Acceptance: PostgreSQL/pgvector, Redis, API, worker, and web start with persistent paths and health endpoints.
  - Verify: `make up && make doctor` passes on the supported profile.
  - Dependencies: Task 3.
- [ ] **Task 5: Implement persistence, jobs, storage, audit/outbox, and backup-inventory ports**
  - Acceptance: migrations, idempotent jobs, manifest storage, transactional audit/outbox behavior, and deletion-aware managed-backup inventory contracts are tested.
  - Verify: `make test-integration` passes restart/retry/rollback cases.
  - Dependencies: Task 4.

### Checkpoint A

- [ ] Foundation build, coverage, health, persistence, and recovery gates pass.

## Phase 2 — Identity and catalog

- [ ] **Task 6: Implement owner setup, sign-in, and secure sessions**
  - Acceptance: first-run wizard verifies database, Redis, storage, and Ollama; first owner can initialize/sign in; failures are actionable and resumable.
  - Verify: API tests plus Playwright success/unavailable-database/unwritable-storage/missing-model scenarios on three device classes.
  - Dependencies: Task 5.
- [ ] **Task 7: Implement users, roles, collections, and grants**
  - Acceptance: owner/admin/member permissions are enforced in services/queries and access changes emit transactional audit events.
  - Verify: security tests prove cross-collection and direct-ID denial.
  - Dependencies: Task 6.
- [ ] **Task 8: Implement source catalog and responsive application shell**
  - Acceptance: authorized users see permitted sources; navigation works on mobile/tablet/desktop.
  - Verify: unit/API tests and Playwright responsive catalog scenarios pass.
  - Dependencies: Tasks 6–7.

## Phase 3 — First RAG slice

- [ ] **Task 9: Register uploads and store original files safely**
  - Acceptance: checksum, size/type validation, manifest, and duplicate warning work.
  - Verify: upload integration tests cover valid, corrupt, encrypted, oversized, and duplicate inputs.
  - Dependencies: Task 8.
- [ ] **Task 10: Define AI provider contracts and Ollama adapters**
  - Acceptance: generation and embedding are independent; missing model/unavailable host errors are typed.
  - Verify: contract tests use deterministic fakes plus optional Ollama smoke test.
  - Dependencies: Tasks 3–4.
- [ ] **Task 11: Extract and normalize PDF, TXT, and Markdown**
  - Acceptance: safe fixtures produce ordered text and citation locations; failures remain non-active.
  - Verify: parser fixture tests and worker retry tests pass.
  - Dependencies: Tasks 5, 9.
- [ ] **Task 12: Chunk, embed, and activate the first index generation**
  - Acceptance: deterministic chunks and compatible vectors activate atomically.
  - Verify: unit/integration tests cover empty content, dimension mismatch, and failed activation.
  - Dependencies: Tasks 10–11.
- [ ] **Task 13: Retrieve authorized evidence with hybrid search**
  - Acceptance: semantic/keyword results merge and rerank under workspace/grant/version filters.
  - Verify: retrieval evaluation and security tests meet initial thresholds with zero leakage.
  - Dependencies: Tasks 7, 12.
- [ ] **Task 14: Generate answers and validate citations**
  - Acceptance: grounded answers cite supplied evidence; weak evidence bypasses generation; typed conversation/query-rewrite and streaming/cancellation contracts exist.
  - Verify: unit/contract tests cover valid, malformed, hallucinated, timed-out, cancelled, and reconnect/error-frame responses.
  - Dependencies: Tasks 10, 13.
- [ ] **Task 15: Deliver the responsive Ask experience**
  - Acceptance: member asks, streams answer, opens citation, filters sources, and sees provider/privacy state on all device classes.
  - Verify: Playwright happy/unhappy projects pass at required widths/orientations.
  - Dependencies: Tasks 8, 14.

### Checkpoint B — Private alpha

- [ ] Upload-to-cited-answer works with Ollama on phone, tablet, and desktop.

## Phase 4 — Knowledge lifecycle

- [ ] **Task 16: Replace and roll back immutable source versions**
  - Acceptance: admins can archive/unarchive with immediate retrieval exclusion; old version stays active until validated cutover; rollback creates a new version; each transition is audited transactionally.
  - Verify: concurrency/integration tests prove no mixed or partial knowledge.
  - Dependencies: Tasks 9–14.
- [ ] **Task 17: Permanently delete and verify source data**
  - Acceptance: owner-only deletion excludes immediately, invokes the Task 5 managed-backup purge port, purges manifests/vectors/conversations, and records zero-count evidence with a transactional audit event.
  - Verify: security/integration/E2E tests cover denial, partial failure, and idempotent retry.
  - Dependencies: Task 16.
- [ ] **Task 18: Rebuild embedding generations safely**
  - Acceptance: transactional outbox catches activation/deletion during rebuild and atomic cutover occurs only after convergence.
  - Verify: crash/concurrency tests prove candidate completeness or safe refusal.
  - Dependencies: Tasks 5, 12, 16–17.

### Checkpoint C

- [ ] Replacement, rollback, deletion, and index migration invariants pass with 100% critical branch coverage.

## Phase 5 — Ingestion breadth and websites

- [ ] **Task 19: Add DOCX, PPTX, XLSX, CSV, image, and OCR adapters**
  - Acceptance: every supported format has location-aware fixtures and actionable failures.
  - Verify: parser/security fixtures pass resource-limit and malformed-content cases.
  - Dependencies: Task 11.
- [ ] **Task 20: Crawl websites safely and create immutable snapshots**
  - Acceptance: sitemap/start URL, allowlists, limits, checksums, and content-addressed reuse feed the same source-version/index contracts as uploads.
  - Verify: integration/security tests cover private IP, redirect escape, timeout, and partial crawl.
  - Dependencies: Tasks 5, 11–13, 16, 18.
- [ ] **Task 21: Schedule website synchronization and expose freshness**
  - Acceptance: unchanged pages reuse artifacts; removed pages retire only after a complete indexed snapshot activates through the source lifecycle/outbox.
  - Verify: clock-controlled jobs plus crawl-to-retrieval E2E prove failure retains the prior snapshot and cutover excludes removed pages.
  - Dependencies: Tasks 16, 18, 20.

## Phase 6 — Operations and provider choice

- [ ] **Task 22: Add activity, retry, source health, and diagnostics**
  - Acceptance: authorized operators query the audit events emitted since Task 5, see sanitized progress/errors, and retry only safe jobs.
  - Verify: API/UI tests cover queued/running/retrying/failed/completed states.
  - Dependencies: Tasks 5, 8, 21.
- [ ] **Task 23: Add GLM generation configuration and consent**
  - Acceptance: owner explicitly accepts distinct generation/rewrite/history payloads; GLM implements generation and `QueryRewriter`; switch requires no re-index and never silently fails over.
  - Verify: provider contract/security/Playwright tests cover invalid key, outage, and consent rejection.
  - Dependencies: Tasks 10, 14–15.
- [ ] **Task 24: Add encrypted backup and clean restore**
  - Acceptance: the real backup implementation satisfies Task 5 inventory/purge ports; owner creates/restores encrypted manifests; a live deletion/backup integration proves deleted-source archives are purged.
  - Verify: clean-install tests cover correct key, wrong key, corruption, incompatibility, and retrieval smoke test.
  - Dependencies: Tasks 5, 17–18.
- [ ] **Task 25: Add feedback and unanswered-question analytics**
  - Acceptance: helpful feedback and privacy-safe aggregate diagnostics are visible by role.
  - Verify: unit/API/Playwright tests cover empty, populated, restricted, and redacted states.
  - Dependencies: Tasks 7, 14–15.

### Checkpoint D

- [ ] All supported sources, GLM switching, operations, and restore workflows pass.

## Phase 7 — Responsive, security, and performance hardening

- [ ] **Task 26: Complete responsive and accessibility hardening**
  - Acceptance: every role/workflow passes at 320/768/1024/1440 widths, mobile/tablet orientations, keyboard, touch, and WCAG AA checks.
  - Verify: complete Playwright matrix and automated accessibility scan pass.
  - Dependencies: all user-facing tasks.
- [ ] **Task 27: Complete security, performance, and RAG quality hardening**
  - Acceptance: abuse tests, performance targets, and blocking evaluation metrics pass on the recommended profile.
  - Verify: `make test-security && make test-performance && make test-evaluation && make coverage` passes with fixed/reported random seeds.
  - Dependencies: Tasks 18–26.

## Phase 8 — Packaging and pilot release

- [ ] **Task 28: Package, document, and verify the pilot release**
  - Acceptance: clean-machine setup, safe upgrade/downgrade refusal, graceful shutdown, LAN profile, measured model profiles, support bundle, release evidence, rollback, and design-partner usability are documented and tested.
  - Verify: immutable `make check` evidence includes production-build Playwright artifacts and dependency/container scans for the exact packaged commit.
  - Dependencies: Task 27.

### Checkpoint E — Pilot-ready MVP

- [ ] All canonical MVP completion criteria pass and release evidence is retained.
