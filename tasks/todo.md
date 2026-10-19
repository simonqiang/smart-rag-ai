# Smart RAG AI MVP Task List

Each task follows TDD, includes happy and unhappy paths, and must keep all repository quality gates green. Detailed steps and interfaces are in [the execution plan](../docs/superpowers/plans/2026-10-05-smart-rag-mvp-implementation.md).

Closed decisions (platforms, languages, models, limits, job dispatch, reranking, E2E cadence) are in [spec §15](../docs/superpowers/specs/2026-10-04-smart-rag-local-first-design.md). A parallel customer-discovery track is defined in [plan.md](./plan.md).

These are master work packages. Before implementation, any package larger than one focused session must be expanded here into 1–2 hour child tasks while preserving its acceptance criteria, dependencies, and interface contract.

## Phase 0 — Decisions and evaluation

- [x] **Task 1: Pin the supported development profile and limits**
  - Acceptance: Windows 11 x86_64, macOS Apple Silicon, and Ubuntu LTS x86_64 profiles; dependency versions; `qwen3:8b`/`bge-m3` (and compact `qwen3:4b`) model profiles; 50 MB upload limit; and owner-confirmed page/crawl/retention limits (spec §15) are recorded and machine-checkable. Includes the minimal `pyproject.toml`/`Makefile` bootstrap.
  - Verify: `make doctor` reports the profile and fails clearly, with platform-specific remediation, for missing requirements.
  - Dependencies: none.
- [x] **Task 2: Create the RAG evaluation dataset and scorer contract**
  - Acceptance: positive, negative, ambiguous, stale, and unauthorized fixtures in English, Chinese, Malay, and mixed-language content calculate every blocking metric per stratum (English, `zh-Hans`, `zh-Hant`, Malay, mixed); grounded-answer acceptance replays digest-bound recorded responses through the current pipeline against human labels.
  - Verify: `make test-evaluation` produces a deterministic per-language and aggregate baseline report.
  - Dependencies: Task 1.

## Phase 1 — Foundation

- [x] **Task 3: Scaffold the monorepo and blocking quality pipeline**
  - Acceptance: web/API/worker packages build; every required lint/type/unit/integration/security/performance/E2E smoke/evaluation/coverage/build/licence/scan target runs through `make check` in CI; `make check-release` and the nightly workflow run the complete Playwright matrix and Windows/macOS/Linux install smoke tests.
  - Verify: `make check` passes; an intentional uncovered branch makes `make coverage` fail.
  - Dependencies: Task 1.
- [x] **Task 4: Start and diagnose the local Compose stack**
  - Acceptance: PostgreSQL/pgvector, Redis, API, worker, and web start with persistent paths and health endpoints.
  - Verify: `make up && make doctor` passes on the supported profile.
  - Dependencies: Task 3.
- [x] **Task 5: Implement persistence, jobs, storage, audit/outbox, and backup-inventory ports**
  - Acceptance: migrations, idempotent jobs dispatched to Dramatiq only from committed PostgreSQL outbox entries, manifest storage, transactional audit/outbox behavior, and deletion-aware managed-backup inventory contracts are tested.
  - Verify: `make test-integration` passes restart/retry/rollback cases.
  - Dependencies: Task 4.

### Checkpoint A

- [x] Foundation build, coverage, health, persistence, and recovery gates pass.

## Phase 2 — Identity and catalog

- [x] **Task 6: Implement owner setup, sign-in, and secure sessions** (expanded into child tasks 6a–6f; all complete)
  - Acceptance: first-run wizard verifies database, Redis, storage, and Ollama; first owner can initialize/sign in; invitation-token and temporary-password primitives (with forced password change) exist for Task 7 to manage; sign-in failures are throttled; `make reset-owner-password` recovers owner access on the host with an audit event; failures are actionable and resumable.
  - Verify: API tests plus Playwright success/unavailable-database/unwritable-storage/missing-model/invitation scenarios on three device classes; integration test for host-local owner recovery.
  - Dependencies: Task 5.
  - Child tasks (interfaces and acceptance criteria preserved from the master package):
    - [x] **6a: Identity schema and first-owner primitive** — Alembic migration for `workspaces`/`users`/`sessions`/`invitations`; Argon2id password hashing; `create_first_owner` with first-owner exclusivity (refuses when any user exists), workspace row, and transactional `workspace.initialized` audit event via `EventWriter`. Verify: `tests/integration/identity_access/test_first_owner.py` covers success, exclusivity, rollback atomicity.
    - [x] **6b: Authentication, throttling, and sessions** — `authenticate` (Argon2id verify, wrong-password failure), per-user sign-in failure throttling with lockout, session issue/`require_session` with expiry and revocation, secure cookie attributes. Verify: focused unit/integration auth tests cover wrong password, lockout, expired/revoked session.
    - [x] **6c: Setup and session API routes** — `check_setup_dependencies` (database, Redis, storage, Ollama — actionable, resumable), resumable `initialize_workspace`, `POST /api/setup/owner`, `/api/session` login/logout/me, secure cookies + Origin check on mutations, actionable failure payloads. Verify: `tests/api/test_setup_routes.py` success/unavailable-dependency/resume/second-owner-refused.
    - [x] **6d: Invitation and temporary-password primitives** — `issue_invitation_token`, `accept_invitation` (expired/reused/revoked refused), `require_password_change`, `POST /api/invitations/accept`, forced password change on temporary passwords. Verify: API tests cover accept/expired/reused/revoked/forced-change flows.
    - [x] **6e: Host-local owner recovery** — `reset_owner_password` in `infra/scripts/reset_owner_password.py`, `make reset-owner-password`; revokes sessions, writes audit event, preserves data, refuses non-owning OS accounts and password arguments/env vars, never logs the password. Verify: `tests/integration/identity_access/test_owner_recovery.py`.
    - [x] **6f: Setup/sign-in web pages and first-run E2E** — `apps/web/src/features/auth/SetupPage.tsx` + sign-in page with actionable/resumable states; Playwright `e2e/first-run.spec.ts` success/unavailable-database/unwritable-storage/missing-model scenarios at mobile/tablet/desktop. Verify: full first-run Playwright matrix passes.
- [x] **Task 7: Implement users, roles, collections, and grants** (expanded into child tasks 7a–7d; all complete)
  - Acceptance: owner/admin/member permissions are enforced in services/queries; owners/admins invite users with role and collections, revoke invitations, reset passwords per role rules, and disable users; access changes emit transactional audit events.
  - Verify: security tests prove cross-collection and direct-ID denial.
  - Dependencies: Task 6.
  - Child tasks (interfaces and acceptance criteria preserved from the master package):
    - [x] **7a: Grants schema and management primitives** — Migration `0003` for `collection_grants`; `grants.py` with `set_user_collections` (atomic replace + audit), `disable_user` (status flip + session revocation + audit, refuses owners/self), `TargetNotFound` for unknown/cross-workspace targets; `accept_invitation` inserts invitation-collection grants atomically with the user row. Verify: `tests/integration/identity_access/test_grants.py`.
    - [x] **7b: AccessContext and authorize** — `authorization.py` with `AccessContext`, `authorize(action, resource, context)` (owner > admin > member; admin manages members only; nobody manages owners except host recovery), `can_access_collection`, `accessible_collection_ids(user_id, workspace_id)` (owner/admin unrestricted, members grant-scoped). Verify: `tests/unit/identity_access/test_authorization.py` plus role-rule cases in `test_grants.py`.
    - [x] **7c: User administration API** — `apps/api/routes/users.py`: list users, invite (role + collections), revoke invitation, set collections, reset password per role rules, disable; unknown/cross-workspace IDs and nonexistent IDs return the identical generic 404; role denials are 403; `must_change_password` sessions are limited to password change. Verify: `tests/api/test_user_routes.py`.
    - [x] **7d: User access UI and isolation security tests** — `apps/web/src/features/settings/UserAccessPage.tsx` (list, invite, revoke, collections, reset, disable) with component tests; `tests/security/test_collection_isolation.py` proves cross-collection and direct-ID denial. Verify: `make test-security`, `npm run test -w apps/web`.
- [x] **Task 8: Implement source catalog and responsive application shell** (expanded into child tasks 8a–8d; all complete)
  - Acceptance: authorized users see permitted sources; navigation works on mobile/tablet/desktop.
  - Verify: unit/API tests and Playwright responsive catalog scenarios pass.
  - Dependencies: Tasks 6–7.
  - Child tasks (interfaces and acceptance criteria preserved from the master package):
    - [x] **8a: Catalog schema and service** — Migration `0004` for `collections` and `sources` (state defaults `active`); `src/source_catalog/catalog.py` with `create_collection`, `create_source`, `list_sources`, `get_source`, `SourceNotFound`/`CollectionNotFound` (unknown/cross-workspace/ungranted indistinguishable); `accessible_collection_ids` enumerates workspace collections for owner/admin, replacing the wildcard. Verify: `tests/integration/source_catalog/test_catalog.py` plus updated `test_grants.py`.
    - [x] **8b: Catalog API routes** — `apps/api/routes/sources.py`: `GET /api/sources` (grant-filtered), `POST /api/sources` (owner/admin), `GET /api/sources/{id}` (direct-ID grant check, generic 404), `GET/POST /api/collections`. Member contexts built from `accessible_collection_ids`. Verify: `tests/api/test_source_routes.py`.
    - [x] **8c: App shell and source list UI** — `apps/web/src/AppShell.tsx` (responsive nav, 44px touch targets, no hover-only controls) and `apps/web/src/features/sources/SourceListPage.tsx` (empty, loading, error, permitted list, restricted hint); route wiring in `App.tsx`. Verify: `npm run test -w apps/web`, `tsc --noEmit`.
    - [x] **8d: Responsive catalog E2E** — `e2e/source-catalog.spec.ts` with mocked API: empty state, permitted list, restricted source denial, loading/error states, navigation on the smoke device matrix; assert no page-level horizontal overflow and no hover-only actions. Verify: `npx playwright test e2e/source-catalog.spec.ts`.

## Phase 3 — First RAG slice

- [x] **Task 9: Register uploads and store original files safely** (expanded into child tasks 9a–9c; all complete)
  - Acceptance: checksum, size/type validation, manifest, and duplicate warning work.
  - Verify: upload integration tests cover valid, corrupt, encrypted, oversized, and duplicate inputs.
  - Dependencies: Task 8.
  - Child tasks (interfaces and acceptance criteria preserved from the master package):
    - [x] **9a: Upload registration service** — Migration `0005` for `source_versions` (state `uploaded`, object sha256, size, media type); `src/ingestion/uploads.py` with `register_upload(UploadRequest, AccessContext) -> UploadAccepted` — 50 MB cap, PDF magic + `/Encrypt` sniff, unsupported-type refusal with reason, content-addressed `ObjectStore` storage + manifest, duplicate-checksum warning, atomic source + version + `source.created`/`source.uploaded` audits, owner/admin only, collection must exist in the caller's workspace. Verify: `tests/integration/ingestion/test_uploads.py` (valid, duplicate, oversized, corrupt, encrypted, unsupported, unauthorized).
    - [x] **9b: Upload API route** — `apps/api/routes/uploads.py`: `POST /api/uploads` (multipart: file, collection_id, name) streaming with the size cap enforced while reading, `UploadRejected.reason` mapped to actionable 400/413 payloads, member/unknown-collection cases covered. Verify: `tests/api/test_upload_routes.py`.
    - [x] **9c: Upload UI** — `apps/web/src/features/sources/UploadSource.tsx` (collection select, name, file input, duplicate warning + rejection reasons displayed) reachable from `SourceListPage` for admins. Verify: component tests + `tsc --noEmit`; invalid content never becomes active (service-level assertions).
- [x] **Task 10: Define AI provider contracts and Ollama adapters**
  - Acceptance: generation and embedding are independent; missing model/unavailable host errors are typed.
  - Verify: contract tests use deterministic fakes plus optional Ollama smoke test.
  - Dependencies: Tasks 3–4.
- [x] **Task 11: Extract and normalize PDF, TXT, and Markdown**
  - Acceptance: safe English/Chinese/Malay/mixed fixtures produce ordered, language-tagged text and citation locations; failures remain non-active.
  - Verify: parser fixture tests and worker retry tests pass.
  - Dependencies: Tasks 5, 9.
- [x] **Task 12: Chunk, embed, and activate the first index generation**
  - Acceptance: deterministic chunks (including CJK), language-aware keyword vectors, and compatible vectors activate atomically.
  - Verify: unit/integration tests cover empty content, dimension mismatch, and failed activation.
  - Dependencies: Tasks 10–11.
- [x] **Task 13: Retrieve authorized evidence with hybrid search**
  - Acceptance: semantic/keyword results merge with reciprocal-rank fusion under workspace/grant/version filters; Chinese segmenter selected by evaluation and approved before being added.
  - Verify: retrieval evaluation meets thresholds for each language and security tests show zero leakage.
  - Note: Chinese keyword segmentation uses dependency-free character bigrams (spec §15 "segmented in the application"); no production dependency was added, so no owner approval was required. Swap for an approved segmenter if evaluation shows bigram recall below threshold.
  - Dependencies: Tasks 7, 12.
- [x] **Task 14: Generate answers and validate citations** (expanded into child tasks 14a–14c; all complete)
  - Acceptance: grounded answers in the question's language cite supplied evidence; weak evidence bypasses generation; typed conversation/query-rewrite and streaming/cancellation contracts exist.
  - Verify: unit/contract tests cover valid, malformed, hallucinated, timed-out, cancelled, and reconnect/error-frame responses.
  - Dependencies: Tasks 10, 13.
  - Note (15): the ask/answer toggle is one stable button; cancel defers past the click's default action because flipping type=button→submit mid-click makes the browser submit the form.
  - Note: citation markers are `[N:"verbatim quote"]`; validation enforces label-within-evidence and quote-inside-cited-span (spec citation-correctness rule). Cancellation = client abort closes the SSE body, cancelling generation at the next chunk boundary; the client renders its own cancelled state.
  - Child tasks (interfaces and acceptance criteria preserved from the master package):
    - [x] **14a: Conversation schema and store** — Migration `0007` for `conversations`/`conversation_messages`; `src/retrieval_answering/conversation.py` with `ConversationContext` (bounded four messages), create/load/list/rename/delete scoped to owner user + workspace (unknown/cross-workspace indistinguishable), `delete_expired_conversations` for retention, `find_message_by_request` for resume. Verify: `tests/integration/retrieval_answering/test_conversations.py`.
    - [x] **14b: Grounded answer core and citation validation** — `src/retrieval_answering/citations.py` (`[N:quote]` markers, `validate_citations` enforcing labels within supplied evidence and verbatim quotes inside the cited span per spec §302, typed `MalformedAnswerError`/`UngroundedAnswerError`) and `src/retrieval_answering/answering.py` (`QueryRewriter` port, `answer`, `stream_answer`, typed `AnswerStreamEvent` `started|delta|citation|completed|error|cancelled` with request/resume identifiers, language policy, insufficient-evidence bypass without a model call, safe error frames). Ollama NDJSON `stream_generate`; streaming fake. Verify: `tests/unit/retrieval_answering/test_answering.py` plus Ollama streaming contract test.
    - [x] **14c: Ask API routes** — `apps/api/routes/ask.py`: `POST /api/ask` (rewrite → retrieve → answer; JSON or SSE via Accept), `GET /api/ask/{request_id}/events` resume replay, audit-safe metadata, provider/privacy badge, error mapping. Verify: `tests/api/test_ask_routes.py`.
- [x] **Task 15: Deliver the responsive Ask experience** (expanded into child tasks 15a–15d; all complete)
  - Acceptance: member asks, streams answer, opens citation, filters sources, lists/reopens/deletes conversations, and sees provider/privacy state on all device classes.
  - Verify: Playwright happy/unhappy projects pass at required widths/orientations.
  - Dependencies: Tasks 8, 14.
  - Child tasks (interfaces and acceptance criteria preserved from the master package; plan's `packages/contracts` package does not exist in this scaffold — the Ask contracts live in `apps/web/src/features/ask/contracts.ts`):
    - [x] **15a: Ask contracts, streaming hook, and Ask page** — `apps/web/src/features/ask/contracts.ts` (Ask/SSE frame types), `useAsk.ts` (fetch-SSE streaming with cancel + error frames), `AskPage.tsx` (question input, streaming answer with citation markers, insufficient-evidence state, provider/privacy badge, source filter, feedback hook). Verify: component tests + `tsc --noEmit`.
    - [x] **15b: Evidence panel** — `apps/web/src/features/ask/EvidencePanel.tsx` opening a citation (source, location, source-language quote) from answer markers, adaptive placement, no hover-only interaction. Verify: component tests + `tsc --noEmit`.
    - [x] **15c: Conversation list and routes** — `apps/api/routes/conversations.py` (list/reopen/rename/delete own conversations) + `apps/web/src/features/ask/ConversationList.tsx` with citation re-authorization on reopen. Verify: API tests, component tests.
    - [x] **15d: Ask E2E matrix** — `e2e/ask.spec.ts`: happy stream, English/Chinese/Malay `lang` attributes, conversation list/reopen/delete, cancellation, reconnect/error frames, citation open, insufficient evidence, provider timeout, restricted source, empty state, keyboard/touch, no console errors/clipping/overflow on every device project. Verify: `npx playwright test e2e/ask.spec.ts`.

### Checkpoint B — Private alpha

- [x] Upload-to-cited-answer works with Ollama in English, Chinese, and Malay on phone, tablet, and desktop.
  - Verified 2026-10-08 on the live Compose stack: three uploads indexed through the real dispatcher/worker (bge-m3), then /api/ask over SSE answered in English, zh-Hans, and Malay, each with validated citations to the matching source; Playwright live spec (e2e/live-ask.spec.ts, guarded by SMART_RAG_LIVE_URL) passed sign-in → ask → citation-open on Pixel 7 portrait, iPad Mini landscape, and Desktop Chrome with no console errors or overflow.
  - Findings fixed on the way: worker broker ignored settings (dramatiq default), outbox relay was never wired, uploads never enqueued extraction, extraction never chained indexing, plainto_tsquery AND-semantics zeroed the Chinese keyword channel, and qwen3 thinking stalled CPU generation. Deferred to Task 27: model declinations without citation markers surface as malformed_answer error frames, and the confidence threshold needs evaluation-driven tuning (the OR keyword channel admits weak double-channel matches).

## Phase 4 — Knowledge lifecycle

- [x] **Task 16: Replace and roll back immutable source versions**
  - Acceptance: admins can archive/unarchive with immediate retrieval exclusion; old version stays active until validated cutover; rollback creates a new version; each transition is audited transactionally.
  - Verify: concurrency/integration tests prove no mixed or partial knowledge.
  - Dependencies: Tasks 9–14.
- [x] **Task 17: Permanently delete and verify source data**
  - Acceptance: owner-only deletion excludes immediately, invokes the Task 5 managed-backup purge port, purges manifests/vectors/citation snapshots, replaces affected assistant-message content with a deletion notice (other messages and conversation records remain), and records zero-count evidence with a transactional audit event.
  - Verify: security/integration/E2E tests cover denial, partial failure, and idempotent retry.
  - Dependencies: Task 16.
  - Reverified 2026-10-09: `make check` passed (451 tests, 100% changed-line coverage, smoke device matrix). Tombstones cannot be reversed, replacement writes serialize with deletion, and incomplete purges can be retried; the durable backup adapter remains Task 24.
- [x] **Task 17A: Purge expired knowledge by retention policy**
  - Acceptance: owner sets retention; scheduled purge removes expired superseded versions, retired pages, unreferenced artifacts, and expired conversations through the Task 17 purge path, never touching active knowledge; candidates are claimed under lock and rechecked before every destructive step; each run audits counts only.
  - Verify: integration tests cover expiry, shortened retention, active-never-purged, purge-versus-rollback/activation/snapshot-reuse concurrency, partial failure/retry, and non-owner denial; Playwright smoke for the retention screen.
  - Dependencies: Tasks 16–17.
  - Status 2026-10-16 (complete): migration `0010` (`retention_policies`, spec §15 defaults 30/90 days); `src/source_catalog/retention.py` (owner-only `update_retention_policy` via new `retention.update` action, idempotent `run_retention_purge`, `purge_version_if_expired` claims each candidate under `FOR UPDATE` and rechecks state+cutoff before deleting). Versions in `superseded`/`retired` (Task 20 pages reuse the state) purge with their chunks and generations; artifacts delete only when no version row references the sha and the file predates the cutoff (mtime guard closes the upload-in-flight race, `ponytail:` noted for S3); conversations expire through `delete_expired_conversations` (now workspace-scoped). Relay enqueues one `retention_purge` job per UTC day from a committed outbox row; scheduling never gates dispatch. Owner-only `GET|PUT /api/settings/retention` + `RetentionSettings.tsx` at `/settings/retention`. Tests: tests/integration/source_catalog/test_retention.py (20), tests/api/test_retention_routes.py (3), e2e/retention.spec.ts (27 across 9 device projects). `make check` green 2026-10-16 (502 passed, diff coverage 100%).
  - Child tasks (interfaces and acceptance criteria preserved from the master package):
    - [x] **17A-a: Retention schema, policy service, and purge core** — Migration `0010` for `retention_policies` (per-workspace `superseded_days` 30 / `conversation_days` 90 defaults, spec §15); `src/source_catalog/retention.py` with `RetentionPolicy`, `get_retention_policy` (defaults when absent), owner-only `update_retention_policy` (audit `retention.updated`), idempotent `run_retention_purge` (per-workspace cutoffs; expired `superseded`/`retired` versions claimed under `FOR UPDATE` and rechecked before each destructive step; Task 17 reference counting before manifest delete; unreferenced-artifact scan guarded by object age; conversation expiry via `delete_expired_conversations`; counts-only `retention.purged` audit; live-knowledge counts verified unchanged). Verify: `tests/integration/source_catalog/test_retention.py` (expiry, shortened retention, active-never-purged, purge vs rollback/activation/reuse, partial failure/retry, counts-only audit).
    - [x] **17A-b: Worker task, daily schedule, and API routes** — `apps/worker/tasks/retention_purge.py` (actor, claim/retry on `RetentionIncomplete`); relay enqueues one `retention_purge` job per UTC day from a committed outbox row (idempotency key); `apps/api/routes/settings.py` `GET|PUT /api/settings/retention` (owner only, 403 otherwise). Verify: `tests/api/test_retention_routes.py` (owner read/update, admin/member denial, validation).
    - [x] **17A-c: Retention settings screen** — `apps/web/src/features/settings/RetentionSettings.tsx` (owner edits both retentions, save + notice/error states) wired at `/settings/retention` with nav entry. Verify: component tests + `tsc --noEmit`.
    - [x] **17A-d: Retention E2E smoke and gate** — `apps/web/e2e/retention.spec.ts` (owner loads/edits/saves, non-owner 403 message, no overflow/console errors). Verify: `npx playwright test e2e/retention.spec.ts`; `make check` green; todo/plan updated.
- [x] **Task 18: Rebuild embedding generations safely**
  - Acceptance: transactional outbox catches activation/deletion during rebuild and atomic cutover occurs only after convergence.
  - Verify: crash/concurrency tests prove candidate completeness or safe refusal.
  - Dependencies: Tasks 5, 12, 16–17.
  - Status 2026-10-15 (complete): `src/knowledge_index/rebuild.py` (start_rebuild/apply_outbox_event/drain_outbox/stage_candidate/verify_candidate/activate_candidate), worker `apps/worker/tasks/rebuild_index.py`, routes `POST|GET /api/index/rebuild` (owner starts, admin observes, member denied), migration `0009` (outbox.seq watermark + index_rebuilds, one running per workspace). Membership is derived, never snapshotted ('active' + 'indexed' of non-deleted sources; archived included so unarchive never exposes a bare source); cutover re-derives under a per-workspace advisory lock that Task 16 cutover and Task 17 deletion requests also take, drains the ledger in the same transaction, verifies exact coverage, retires every non-target generation and activates candidates atomically (indexed replacements keep parity coverage). activate_generation refuses stale-key activations during a rebuild. Tests: tests/integration/knowledge_index/test_rebuild_concurrency.py (29, incl. 10-seed deterministic interleaving matrix) + tests/api/test_index_generation_routes.py. `make check` green 2026-10-15 (479 passed, diff coverage 100%).

### Checkpoint C

- [x] Replacement, rollback, deletion, retention purge, and index migration invariants pass with 100% critical branch coverage.
  - Verified 2026-10-16: `make check` green (502 tests, per-module 96% gate, 100% diff coverage). Failed replacement keeps old knowledge active; cutover excludes it; deletion reaches verified zero residue with retryable incomplete purges; retention purge removes expired superseded/retired versions, unreferenced artifacts, and stale conversations without touching active knowledge; rebuild cutover serializes with deletion and version activation through the per-workspace advisory lock.

## Phase 5 — Ingestion breadth and websites

- [x] **Task 19: Add HTML, DOCX, PPTX, XLSX, CSV, image, and OCR adapters** (expanded into child tasks 19a–19d; all complete)
  - Acceptance: every supported format has location-aware fixtures and actionable failures; OCR covers English, Chinese (Simplified/Traditional), and Malay.
  - Verify: parser/security fixtures pass resource-limit and malformed-content cases.
  - Dependencies: Task 11.
  - Decisions (2026-10-19): office formats parse via the already-bundled python-docx/python-pptx/openpyxl (docling's backends lose slide provenance and keep nav/footer boilerplate, so direct libs win on both acceptance points); HTML/CSV use the stdlib; OCR uses Tesseract with the spec §15 packs (`eng`, `chi_sim`, `chi_tra`, `msa`) via pytesseract. Docling's RapidOCR was rejected: onnxruntime is not installed and spec §15 pins Tesseract. Scanned-PDF handling stays as in Task 11 (empty warning); no PDF pipeline upgrade in this task. OCR script choice: tesseract OSD and confidence-of-script heuristics proved unreliable (each Chinese pack normalizes output toward its own script), so Chinese images run both `chi_sim+eng` and `chi_tra+eng` passes and the higher total word confidence wins (ties prefer chi_sim); arbitration correctness is covered by fake-TSV tests, fixture tests assert script-neutral content. `ponytail:` noted in ocr.py — margins are thin on clean renders; Task 27 evaluation may swap the engine or add a per-collection language hint.
  - Child tasks (interfaces and acceptance criteria preserved from the master package):
    - [x] **19a: HTML parser shared with Task 20** — `src/ingestion/parsers/html.py` (stdlib parser: script/style/noscript/template dropped, nav/header/footer/aside boilerplate skipped, meta-charset sniffing with the existing UTF-8/GB18030/Big5 cascade, headings as own blocks); registered `text/html` in extraction `_PARSERS` and `.html/.htm` in upload `MEDIA_TYPES`. Verify: 13 tests in `tests/integration/ingestion/test_business_formats.py` (scripts/boilerplate removal, GB18030 meta charset, malformed markup, empty page, block locations, upload acceptance). Committed 2026-10-19, module coverage 100%.
    - [x] **19b: DOCX and PPTX adapters** — `src/ingestion/parsers/office.py` (python-docx paragraphs/tables via `iter_inner_content`; python-pptx slides as pages with grouped shapes and slide tables); `archives.py` zip guard (500 MB decompressed, 10k entries, encrypted-entry refusal per spec §15); `.docx/.pptx` uploads with PK-magic rejection. Verify: fixtures cover headings, CJK paragraphs, slide locations, corrupt/encrypted zip, archive bomb, 10k-entry bomb. Committed 2026-10-19, module coverage 100%.
    - [x] **19c: XLSX and CSV adapters** — `src/ingestion/parsers/spreadsheet.py` (openpyxl read-only + data-only: sheets as pages, first non-blank row as header, rows rendered `Header=Value` pairs; merged-cell continuations vanish as None; uncached formulas contribute nothing — `ponytail:` noted; stdlib csv with the same renderer); 10k-row and 2k-char truncation warnings via a `ParseResult` carrier in basic.py; `.xlsx/.csv` upload types. Verify: fixtures cover merged cells, uncached formulas, large-sheet truncation, GB18030 CSV, ragged rows, NUL stripping, oversized-field refusal, empty sheets. Committed 2026-10-19, module coverage 100%.
    - [x] **19d: Image OCR in three languages** — `src/ingestion/parsers/ocr.py` (Pillow load with 64 MP cap, EXIF transpose, alpha flattened onto white, 4096px downscale; `chi_sim+eng` first pass, `chi_tra+eng` arbitration for Han text, 120 s timeout; typed `ocr_unavailable`/`ocr_language_missing`/`ocr_failed`/`corrupt_image`/`image_too_large`); committed fixture PNGs (en/zh-Hans/zh-Hant/ms) rendered with macOS fonts and verified against tesseract; `.png/.jpg/.jpeg/.webp/.tif/.tiff/.bmp` uploads with magic-byte checks; tesseract + the four packs in the worker image; `make doctor` gained the `ocr_language_packs` check with per-platform remediation. Status 2026-10-19 (complete): 55 business-format tests (arbitration covered by fake-TSV tests at the `image_to_data` seam; fixture tests assert script-neutral content so tesseract version drift cannot flip them), 27 doctor tests. All four fixture PNGs OCR correctly (en/chi_sim/chi_tra/msa). `make check` green 2026-10-19 (561 tests passed, diff coverage 100%); host needs `brew install tesseract tesseract-lang` for native runs, the Compose worker image ships the packs.
- [x] **Task 20: Crawl websites safely and create immutable snapshots** (expanded into child tasks 20a–20c; all complete 2026-10-19)
  - Acceptance: sitemap/start URL, allowlists, limits, checksums, and content-addressed reuse feed the same source-version/index contracts as uploads.
  - Verify: integration/security tests cover private IP, redirect escape, timeout, and partial crawl.
  - Dependencies: Tasks 5, 11–13, 16, 17A, 18, 19 (shared HTML parser).
  - Decisions (2026-10-19): a website snapshot is ONE source version whose artifact is a content-addressed JSON bundle of the parsed pages (per-page sha256 + page→URL map) with a dedicated media type — fits the single-object `source_versions` schema with no migration, and the page numbers land in the existing chunk `page` column. The bundle stores parser output (shared `parse_html`), not raw HTML, so HTML is parsed exactly once at crawl time. Crawl limits are spec §15 (500 pages, depth 5, 30 min, 10 MB/response). DNS resolution and HTTP fetch are injectable seams so security tests prove zero private-network escape offline; httpx (already a dependency) is the production transport. `stage_snapshot` mirrors `register_upload`'s transactional shape (source + immutable version + audits + committed-outbox `source_extract` dispatch) but skips file sniffing — the bundle is server-generated, and registering the snapshot type as an upload media type would let clients POST hostile bundles as "snapshots". Unchanged-sha reuse = the new version points at the already-stored object; still-active short-circuit is Task 21's scheduling concern.
  - Child tasks (interfaces and acceptance criteria preserved from the master package):
    - [x] **20a: Crawl policy and SSRF guard** — `src/web_sync/crawl_policy.py`: `CrawlPolicy` (allowed hosts, path prefixes, page/depth/time/size limits from spec §15), `validate_url` (http/https only, host allowlist, path allowlist, no userinfo, fragment-free normalization: lowercase host, drop default port, stable percent-encoding, dedupe form), and destination-safety check resolving ALL addresses through an injectable resolver, refusing private/loopback/link-local/reserved/multicast targets. Verify: `tests/security/test_crawl_safety.py` (scheme/host/path denials, normalization equivalence, private/loopback/link-local/reserved refusal, non-all-resolved refusal). Committed 2026-10-19, module coverage 100% (lines+branches).
    - [x] **20b: Crawler engine** — `src/web_sync/crawler.py`: `crawl(CrawlDefinition, *, resolve, fetch) -> CrawlResult` with injectable transport; sitemap.xml `<loc>` harvest first when given a sitemap start URL (regex `<loc>` extraction — no entity-expansion surface), else BFS over `<a href>` from the start URL; every redirect hop re-validated through the policy (host/path + destination safety); limits enforced (500 pages, depth 5, 30-minute deadline, 10 MB responses, per-request timeout); normalized-URL dedupe; non-HTML responses skipped; pages parsed via the shared HTML parser. Partial crawl yields succeeded pages + warnings; all-pages-failed is a typed failure. Verify: `tests/security/test_crawl_safety.py` crawler cases (private IP, redirect escape, timeout, depth/page/size limits, duplicate URL, partial crawl) against a fake local site. Committed 2026-10-19, module coverage 100% (lines+branches). Notes: pages are recorded under the final redirect URL (each hop re-resolved + re-validated); sitemap URLs must satisfy the path prefixes like any page (host it under the allowlist, e.g. `/docs/sitemap.xml`); `BlockedUrl` converted from frozen dataclass to a plain Exception (frozen instances reject `__traceback__` assignment when raised — HEAD's 20a file was lint-red on SIM102, fixed with a ternary to avoid the coverage.py 3.13 compound-condition quirk).
    - [x] **20c: Snapshots and pipeline staging** — `src/web_sync/snapshots.py`: `build_snapshot` (per-page normalized-content sha256, JSON bundle + page→URL manifest, content-addressed), `stage_snapshot` mirroring `register_upload`'s transactional shape (source row when new, immutable version in `uploaded`, `store.put` object reuse by sha, transactional audits, committed-outbox `source_extract` dispatch); snapshot-bundle parser registered in `_PARSERS`; chunks carry page numbers through the existing `page` column. Verify: integration test proving crawl → stage → extract → index → activate with retrieval honoring existing grant filters, plus unchanged-sha reuse; `make check` green. Status 2026-10-19 (complete): bundle format lives in `src/ingestion/parsers/snapshot.py` (`SNAPSHOT_MEDIA_TYPE` `application/x-smart-rag-website-snapshot`, canonical JSON version 1 with per-page `page_sha256`, `parse_snapshot` → located blocks, `corrupt_snapshot` on malformed input) so the ingestion→web_sync dependency arrow stays one-way; `stage_snapshot` stages new sources only (single transaction: collection check + source + version + `source.created`/`source.uploaded` audits with `start_url`/`pages` + outbox dispatch; replacement resync is Task 21's). Empty crawled pages stay in the bundle (membership matters for Task 21 removed-page detection). Tests: tests/unit/ingestion/test_snapshot_bundle.py (13) + tests/integration/web_sync/test_snapshots.py (7: full chain incl. page numbers in `index_chunks`, grant-filtered retrieval, unchanged-sha reuse = one object file, member denial + generic collection 404, worker-routing corruption). `make check` green 2026-10-19 (628 passed, coverage gate PASS, diff coverage 100%).
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
  - Gate: GLM model, regional endpoint, account type, and data-processing terms recorded in spec §15 before live testing.
  - Dependencies: Tasks 10, 14–15.
- [ ] **Task 24: Add encrypted backup and clean restore**
  - Acceptance: the real backup implementation satisfies Task 5 inventory/purge ports; owner creates/restores encrypted manifests; a live deletion/backup integration proves deleted-source archives are purged; managed backups expire under the Task 17A retention policy and screen.
  - Verify: clean-install tests cover correct key, wrong key, corruption, incompatibility, and retrieval smoke test; expiry tests cover default 30 days, shortened retention, failed deletion/retry, and run counts.
  - Dependencies: Tasks 5, 17, 17A, 18.
- [ ] **Task 25: Add feedback and unanswered-question analytics**
  - Acceptance: helpful feedback and privacy-safe aggregate diagnostics are visible by role.
  - Verify: unit/API/Playwright tests cover empty, populated, restricted, and redacted states.
  - Dependencies: Tasks 7, 14–15.

### Checkpoint D

- [ ] All supported sources, GLM switching, operations, and restore workflows pass.

## Phase 7 — Responsive, security, and performance hardening

- [ ] **Task 26: Complete responsive and accessibility hardening**
  - Acceptance: every role/workflow passes at 320/768/1024/1440 widths, mobile/tablet orientations, keyboard, touch, and WCAG AA checks.
  - Verify: `make test-e2e-full` (complete matrix) and automated accessibility scan pass; nightly workflow runs the same matrix.
  - Dependencies: all user-facing tasks.
- [ ] **Task 27: Complete security, performance, and RAG quality hardening**
  - Acceptance: abuse tests, performance targets, and blocking evaluation metrics pass on the recommended profile.
  - Verify: `make test-security && make test-performance && make test-evaluation && make coverage` passes with fixed/reported random seeds and per-language evaluation thresholds.
  - Dependencies: Tasks 18–26.

## Phase 8 — Packaging and pilot release

- [ ] **Task 28: Package, document, and verify the pilot release**
  - Acceptance: clean-machine setup on Windows 11, macOS (Apple Silicon), and Ubuntu LTS, per-platform prerequisites (including Docker Desktop licence terms), safe upgrade/downgrade refusal, graceful shutdown, LAN profile, measured model profiles, support bundle, release evidence, rollback, and design-partner usability are documented and tested.
  - Verify: immutable `make check-release` evidence includes production-build Playwright artifacts and dependency/container scans for the exact packaged commit.
  - Dependencies: Task 27.

### Checkpoint E — Pilot-ready MVP

- [ ] All canonical MVP completion criteria pass and release evidence is retained.
