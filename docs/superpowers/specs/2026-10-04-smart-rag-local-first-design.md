# Design Specification: Smart RAG AI Local-First MVP

**Date:** 2026-10-04

**Last updated:** 2026-10-05

**Status:** Approved design; implementation plan reviewed 2026-10-05 and awaiting execution approval

**Owner:** Founder/CTO
**Primary audience:** Product owner and implementation engineer

## 1. Objective

Build a local-first RAG web application for SMEs. Owners and administrators upload documents or synchronize website content. Authorized users ask questions and receive evidence-grounded answers with exact citations. Updating a source activates a complete new version and excludes obsolete knowledge; deleting a source removes it from retrieval immediately and purges its stored/derived content through a verified workflow.

The MVP runs on one PC. Ollama provides local generation and embeddings by default. The owner can switch answer generation to the GLM general API without changing code or re-indexing documents. Hosted generation is opt-in and visibly sends the answer policy, current question, selected passage text, citation labels/IDs, and—if enabled—up to four visible conversation messages outside the PC. Optional GLM query rewriting occurs before retrieval and sends only its rewrite policy, the current question, and the same optional conversation-history limit.

Detailed product requirements are in [Product and MVP](../../product-and-mvp.md). UX behavior is in [Application design](../../application-design.md). Architecture and security requirements are in [System architecture](../../system-architecture.md) and [Knowledge lifecycle and security](../../knowledge-lifecycle-and-security.md). Mandatory TDD, coverage, Playwright, and deployment gates are in [Testing and quality strategy](../../testing-and-quality-strategy.md).

## 2. Capability map and build order

| Module id | Responsibility | Depends on |
|---|---|---|
| `foundation` | Repository, configuration, database, jobs, storage, health | — |
| `identity-access` | Accounts, sessions, roles, collection grants | foundation |
| `source-catalog` | Sources, versions, collections, source state | identity-access, foundation |
| `ai-providers` | Ollama generation/embedding and GLM generation adapters | foundation |
| `ingestion` | Upload, extraction, normalization, chunking | source-catalog, foundation |
| `knowledge-index` | Embeddings, hybrid index, generations, activation | ingestion, ai-providers, foundation |
| `retrieval-answering` | Authorized retrieval, reranking, generation, citations | identity-access, knowledge-index, ai-providers |
| `web-sync` | Safe crawling, snapshots, change detection, schedules | source-catalog, ingestion |
| `operations` | Job UI, audit, diagnostics, backup/restore, analytics | all prior modules |

Build order:

```text
foundation
→ identity-access
→ source-catalog + ai-providers
→ ingestion
→ knowledge-index
→ retrieval-answering
→ web-sync
→ operations
```

Each module is independently testable and should receive its own implementation slice. Interfaces are defined by the providing module.

## 3. Functional acceptance criteria

### Installation and setup

- A documented command starts all required application services on a supported PC.
- The first-run wizard creates the owner and verifies storage, database, Redis, and Ollama connectivity.
- Missing local models produce an actionable instruction rather than a stack trace.
- The same documented setup works on each supported operating system in §15.
- An owner who loses their password can recover access only through an audited host-local command; there is no remote reset path.
- Owners and administrators add members with a single-use, expiring invitation or temporary password that must be changed at first sign-in.

### Ingestion

- Supported files are accepted asynchronously and retain original checksum and location metadata.
- Corrupt, encrypted, unsupported, or over-limit content fails safely with a reason.
- A source is not searchable until its complete version passes validation and becomes active.
- Website crawls stay within configured host/path and resource limits.
- English, Chinese (Simplified and Traditional), and Malay content is extracted, searchable by keyword and meaning, and cited correctly, including mixed-language documents.

### Retrieval and answers

- Every retrieval applies workspace, user grant, source state, active-version, and active-index filters.
- Search combines semantic and keyword evidence using reciprocal-rank fusion; a cross-encoder reranker is added only when the evaluation report shows a ranking failure it fixes.
- Answers are written in the language of the question unless the user asks otherwise; citations always quote the source language.
- Factual answers contain validated citations to supplied evidence.
- Weak retrieval returns an insufficient-evidence response.
- Source text cannot instruct the system to ignore policy, expose secrets, or execute tools.

### Freshness and deletion

- Replacing a source leaves its prior version active until the replacement is ready.
- Activating a replacement immediately excludes the superseded version from new retrieval.
- A failed replacement does not disturb the active version.
- Deletion excludes the source from retrieval before asynchronous physical purge.
- Deletion verification confirms no retrievable or stored source content remains, including in application-managed backups.
- Only the owner can permanently delete a source; administrators may archive it.
- Managed backups containing a permanently deleted source are removed before deletion is verified.
- Superseded versions, retired snapshot pages, and conversations are purged automatically when their configured retention expires, using the same manifest-driven purge and verification path as deletion.

### Model providers

- Ollama is the default generation and embedding provider.
- The owner can switch only generation to GLM through validated configuration.
- Switching generation does not re-index documents.
- Changing embedding compatibility creates and validates a new index generation before activation.
- An embedding-index rebuild replays concurrent source changes from a durable ledger and verifies current membership before cutover.
- The system never silently sends local-mode content to GLM.

### Operations

- Administrators can see, diagnose, and safely retry recoverable background jobs.
- Audit events cover access changes, source lifecycle, provider changes, backup, and restore.
- A verified backup restores active knowledge and permissions into a clean compatible installation.
- Managed backups are encrypted, with recovery keys stored separately by the owner.

### Responsive device support

- Every member and administration workflow is fully usable on desktop, tablet/iPad-class, and mobile devices.
- Responsive layouts are verified at minimum at 320 px, 768 px, 1024 px, and 1440 px widths.
- Mobile and tablet portrait/landscape modes preserve every required action and do not introduce unintended page-level horizontal scrolling.
- Touch, keyboard, and pointer input are supported without hover-only controls; touch targets are at least 44 by 44 CSS pixels.

## 4. Technology stack

- React + TypeScript + Vite
- FastAPI + Pydantic + SQLAlchemy + Alembic
- PostgreSQL + `pgvector` + PostgreSQL full-text search
- Redis + Dramatiq workers, dispatched from PostgreSQL job records through the transactional outbox (Redis never receives a job whose record did not commit)
- Local filesystem storage behind a stable object-storage interface
- Ollama for default local generation and embeddings
- GLM general API adapter for optional hosted generation
- Docling-led parsing adapters with local Tesseract OCR (English, Simplified/Traditional Chinese, Malay language packs)
- Language-aware keyword indexing: PostgreSQL `english` and `simple` text-search configurations plus application-side Chinese word segmentation (segmenter selected by evaluation in Task 13)
- pytest, Vitest, Testcontainers, and Playwright
- Docker Compose for the local topology

See [System architecture](../../system-architecture.md) for rationale and boundaries.

## 5. Commands

These are the required command contracts for the implementation. Exact helper targets may be introduced during repository scaffolding, but the resulting interface must remain this simple:

```bash
# First-time local setup and validation
make setup

# Start the complete local application
make up

# Stop services without deleting persistent data
make down

# Run all automated checks (PR gate: Playwright smoke projects)
make check

# Run the release gate (make check plus the full Playwright device matrix)
make check-release

# Run backend tests
make test-api

# Run frontend tests
make test-web

# Run both backend and frontend unit/component suites
make test-unit

# Run browser-level tests
make test-e2e

# Run integration, security, and RAG evaluation suites
make test-integration
make test-security
make test-evaluation

# Enforce backend/frontend and critical-module coverage thresholds
make coverage

# Format and lint supported source files
make lint

# Create a versioned local backup
make backup

# Display service health and sanitized diagnostics
make doctor

# Recover owner access from the host PC (audited; never deletes data)
make reset-owner-password
```

Every command works identically on the supported operating systems in §15. Windows hosts run `make` through the documented WSL2 or Git Bash toolchain; scripts are written in Python rather than shell where portability matters.

No standard command may delete application data. Any future reset command must require an explicit target and confirmation.

## 6. Project structure

```text
apps/
  web/                 React application
  api/                 FastAPI HTTP entrypoint
  worker/              Background worker entrypoint
packages/
  contracts/           Generated/shared API contracts
src/
  identity_access/     Accounts, roles, sessions, grants
  source_catalog/      Sources, versions, collections
  ai_providers/        Generation and embedding interfaces/adapters
  ingestion/           Parsers, normalization, chunking
  knowledge_index/     Embeddings, index generations, activation
  retrieval_answering/ Retrieval, reranking, answers, citations
  web_sync/            Crawl definitions and synchronization
  operations/          Jobs, audit, diagnostics, backup orchestration
  shared/              Cross-cutting primitives with strict scope
tests/
  unit/
  integration/
  security/
  evaluation/
e2e/                   Browser workflows
fixtures/               Safe representative documents and websites
infra/                  Compose, container, and local scripts
docs/                   Product and engineering documentation
```

Modules expose application services and typed contracts. Other modules must not import internal repositories, ORM models, or adapter implementations.

## 7. Code style

Prefer explicit typed services, immutable request objects, and transactional orchestration. Domain code does not read process environment variables directly.

```python
@dataclass(frozen=True)
class ActivateSourceVersion:
    workspace_id: UUID
    source_id: UUID
    version_id: UUID
    actor_id: UUID


class SourceVersionService:
    async def activate(self, command: ActivateSourceVersion) -> None:
        async with self.unit_of_work.transaction():
            version = await self.versions.require_ready(command)
            await self.sources.set_active_version(
                workspace_id=command.workspace_id,
                source_id=command.source_id,
                version_id=version.id,
            )
            await self.audit.record_source_activation(command)
```

Conventions:

- Python modules and functions use `snake_case`; types use `PascalCase`.
- TypeScript variables/functions use `camelCase`; components/types use `PascalCase`.
- Domain identifiers use UUIDs and are never accepted without workspace context.
- Errors are typed and mapped to safe API responses at the boundary.
- Comments explain decisions or invariants, not syntax.
- Logging is structured and applies a central redaction policy.
- Provider-specific fields stay inside provider adapters.

## 8. Testing strategy

All behavior is developed through red–green–refactor TDD. Every new behavior starts with a focused test that fails for the expected reason. Every bug fix starts with a failing reproduction test. The minimum coverage gate is 96% for lines, statements, functions, and branches in every first-party package/module and in both backend and frontend aggregates. Changed executable lines require 100% line and branch coverage. Critical authorization, lifecycle, deletion, index-cutover, provider-privacy, citation, and crawl-policy modules require 100% branch coverage.

Coverage exclusions are limited to generated contracts, vendor code, declarative migrations, and tool configuration, and every exclusion requires review. Coverage does not replace scenario quality or meaningful assertions.

### Unit tests

- Source/version state transitions
- Chunking and location metadata
- Provider request/response mapping
- Retrieval score merging and evidence threshold
- Citation validation
- Crawl allowlist and URL normalization
- Secret/log redaction

### Integration tests

- PostgreSQL transactions and active-version uniqueness
- `pgvector` plus full-text retrieval with authorization filters
- Redis job retry/idempotency behavior
- Parser fixtures for each supported format
- Ollama and GLM adapters against contract fakes; optional live smoke tests are separately marked
- Backup manifest and restore to a clean database

### Security tests

- Cross-workspace and cross-collection retrieval denial
- Prompt-injection fixtures
- SSRF/private-network crawl denial
- Archive/size/parser limits
- Credential and passage redaction
- Deleted and superseded content exclusion

### Evaluation tests

Maintain a versioned dataset of questions, expected relevant sources/passages, and answer criteria. Report:

- retrieval recall at configured `k`;
- ranking quality;
- citation correctness;
- grounded-answer acceptance;
- insufficient-evidence precision;
- stale-content leakage count; and
- latency by hardware/provider profile.

The initial blocking profile requires retrieval recall@10 ≥90%, mean reciprocal rank@10 ≥75%, citation correctness ≥98%, grounded-answer acceptance ≥95%, insufficient-evidence precision ≥90%, insufficient-evidence recall ≥85%, and exactly zero stale/unauthorized retrieval leakage. Evaluation changes are reviewed like code. A model or chunking change does not ship merely because a few examples look better.

Metric measurement is defined so the blocking gate is deterministic:

- Retrieval, ranking, leakage, and insufficient-evidence metrics are computed by code against labelled expected passages and run on every `make test-evaluation`.
- Citation correctness is computed by code: every citation ID must belong to the supplied evidence and its span must contain the quoted claim.
- Grounded-answer acceptance replays recorded provider responses through the current end-to-end answer pipeline on every evaluation and compares the results with human-labelled judgments. Recorded sets are bound to a digest of the prompt, answer pipeline, and model profile; a relevant change invalidates them. An optional local LLM judge may be reported as advisory but never decides a blocking gate.
- Live-model evaluation (new answers from a real Ollama or GLM profile) is a release gate for that model profile, not a per-commit gate; its seed, model digest, and parameters are recorded.
- The dataset contains English, Simplified Chinese, Traditional Chinese, Malay, and mixed-language cases for every case class, and metrics are reported per stratum as well as in aggregate. Each stratum must meet the blocking thresholds independently.

### End-to-end tests

Playwright is the required browser E2E framework. The complete projects, which block nightly and release, cover desktop, tablet/iPad-class, and mobile viewports, including portrait and landscape mobile/tablet orientations. Each critical workflow includes its happy path and relevant unhappy paths:

- first-run setup plus unavailable database, unwritable storage, and missing Ollama model;
- upload to cited answer plus unsupported, corrupt, encrypted, oversized, and parser-failed files;
- successful replacement plus processing failure retaining old knowledge and concurrent-update conflict;
- website sync plus blocked target, timeout, unsafe redirect, and partial crawl;
- grounded answer plus insufficient evidence, provider timeout, invalid response, and citation mismatch;
- permission grant/revocation plus direct unauthorized URL/API/retrieval attempts;
- deletion and verification plus non-owner, partial purge, and idempotent retry cases;
- Ollama-to-GLM generation switch plus invalid credentials, unavailable provider, and no silent hosted fallback; and
- backup/restore plus wrong key, corrupt archive, incompatible version, and deleted-source backup cases.

The blocking Playwright suite uses deterministic local provider fakes. Optional live Ollama and GLM smoke tests are separate and do not replace it. Failed E2E runs retain redacted traces, screenshots, video, console output, and network logs.

E2E cadence:

- **Every pull request / `make check`:** Playwright smoke projects (one mobile portrait, one tablet landscape, one desktop) run every critical workflow's happy path and the unhappy paths touched by the change.
- **Nightly on `main` and `make check-release`:** the complete matrix (320/768/1024/1440 widths, mobile/tablet portrait and landscape, every happy and unhappy path, accessibility scans). A nightly failure blocks further merges until fixed.
- **Packaging:** only a revision that passed `make check-release` may be packaged. The full matrix therefore remains blocking for every release.

## 9. Engineering boundaries

### Always do

- Validate input at every trust boundary.
- Apply workspace, permission, active-version, and deletion filters in retrieval storage queries.
- Make jobs idempotent and observable.
- Preserve the last good source/index until a replacement passes validation.
- Follow red–green–refactor TDD for every behavior change and bug fix.
- Run affected tests and `make check` before merge.
- Update this specification before implementing a changed architectural decision.
- Pin and audit dependencies used to parse untrusted files.
- Check every new dependency's licence is compatible with Apache-2.0 distribution; copyleft network licences (for example AGPL, which covers PyMuPDF) are not allowed in shipped code.

### Ask first

- Change database schema after a module specification is approved.
- Add a production dependency or external service.
- Expand supported file formats or authenticated website crawling.
- Change retention defaults or backup semantics.
- Send additional customer data to a hosted provider.
- Introduce a separate vector database, search engine, or microservice.
- Add telemetry that leaves the installation.

### Never do

- Commit secrets, customer data, generated indexes, or production backups.
- Retrieve first and apply access control after generation.
- Mix vectors from incompatible embedding/index generations.
- Activate a partially processed source snapshot.
- Silently fall back from local to hosted inference.
- Log full prompts, raw sensitive passages, passwords, or API keys by default.
- Execute instructions found inside ingested documents or webpages.
- Remove failing tests merely to unblock delivery.
- Lower coverage thresholds, add unjustified exclusions, or repeatedly rerun flaky tests until they happen to pass.

## 10. Operational requirements

- Health endpoints distinguish liveness from dependency readiness.
- Structured logs include correlation, job, workspace, source, and version identifiers where safe.
- Diagnostics redact secrets and source content.
- Database and file storage expose disk-capacity warnings before exhaustion.
- Startup performs compatible schema checks and refuses unsafe downgrades.
- Graceful shutdown stops accepting new work and returns in-progress jobs to a recoverable state.
- Restore documentation names supported version compatibility and rollback limits.
- The default deployment binds only to loopback. LAN mode exposes only a TLS reverse proxy and uses explicit host, origin, proxy, cookie, and firewall settings.
- Packaging and deployment are blocked unless lint, type checks, unit/component, integration, security, the full Playwright E2E matrix, RAG evaluation, coverage, build, and security scans all pass for the same revision (`make check-release`).
- Every deployable revision has a documented rollback to the last verified revision.

## 11. Performance targets

Targets are measured on the documented recommended 32 GB local profile with a representative pilot corpus:

- cached application pages become interactive within 2 seconds;
- non-AI API operations complete within 500 ms at p95 under normal single-office load;
- retrieval before generation completes within 2 seconds at p95 for the MVP corpus target;
- job status updates appear within 2 seconds;
- provider/model time is measured separately from retrieval time; and
- the system supports at least 100,000 chunks per installation before a scaling review.

Local generation latency varies too widely by hardware/model for a universal promise. Release notes must publish measured model profiles.

## 12. Definition of MVP complete

The MVP is complete only when:

1. all functional acceptance criteria in this specification pass;
2. all supported formats have fixtures and location-aware citation tests;
3. the evaluation report meets the pilot's agreed quality thresholds with zero stale/unauthorized retrieval leakage;
4. setup and restore are verified on a clean machine for each supported operating system in §15;
5. security tests cover permissions, crawl boundaries, prompt injection, secrets, and deletion;
6. the owner can deliberately switch generation between Ollama and GLM without re-indexing;
7. known limitations and hardware profiles are documented; and
8. every first-party package/module and both application aggregates meet the 96% line, statement, function, and branch thresholds, changed executable lines have 100% line/branch coverage, and designated critical modules have 100% branch coverage;
9. Playwright happy and unhappy scenarios pass against the production build; and
10. every permitted workflow passes responsive Playwright coverage on desktop, tablet/iPad-class, and mobile projects; and
11. a design partner can complete the core workflow without developer intervention.

## 13. Deferred decisions

The following decisions remain open until implementation or customer evidence supplies the missing information:

- GLM model identifier, regional endpoint, and commercial data-processing terms;
- packaging approach beyond Docker Compose for non-technical customers; and
- target vertical after design-partner discovery.

Decisions closed on 2026-10-05 are recorded in §15.

These are bounded configuration/product decisions, not gaps in the architectural safety model.

## 14. Pre-implementation decision gates

Deferred decisions must be closed before the dependent implementation task begins:

| Gate | Must be decided before | Status |
|---|---|---|
| Supported operating systems and CPU architectures | Foundation/container scaffolding | Closed — §15 |
| Ollama chat and embedding model profiles | AI-provider contract and performance fixtures | Closed — §15 |
| Supported content languages | Evaluation dataset, extraction, keyword index | Closed — §15 |
| Job queue technology | Foundation jobs (Task 5) | Closed — §15 |
| Reranking approach | Retrieval (Task 13) | Closed — §15 |
| Chinese word segmenter dependency | Retrieval (Task 13) | Open — select by evaluation, then ask before adding |
| GLM model, regional endpoint, account type, and commercial data-processing review | Live GLM adapter testing | Open |
| Upload size limit | Public ingestion contract | Closed — §15 |
| Page, crawl, corpus, and retention defaults | Task 1 profile pinning | Closed — §15 (owner confirmed 2026-10-05) |
| Packaging beyond Docker Compose | Design-partner onboarding work | Open |
| Initial vertical | Vertical-specific templates, terminology, or marketing work | Open — customer discovery track |

The implementation plan must record each decision or explicitly stop before the dependent task. No engineer should infer a default silently.

## 15. Decision record

Decisions closed during the 2026-10-05 plan review:

### Supported platforms

| Platform | Architecture | Container runtime | Ollama | Notes |
|---|---|---|---|---|
| Windows 11 | x86_64 | Docker Desktop (WSL2 backend) | Native Windows app | Most common SME office PC; NVIDIA GPU optional |
| macOS 14+ | Apple Silicon (arm64) | Docker Desktop | Native app (Metal acceleration) | Reference development machine: macOS arm64, 32 GB |
| Linux (Ubuntu 22.04/24.04 LTS) | x86_64 | Docker Engine + Compose plugin | Native service | NVIDIA GPU optional |

Intel Macs, Windows on ARM, and Linux arm64 are not supported for the MVP. Docker Desktop requires a paid subscription for larger organizations under its licence terms; the operator guide must state this and name the tested alternative, if any. CI runs unit/integration suites on Linux and runs `make doctor` plus install smoke tests on all three platforms.

### Content languages

- Supported content: English, Chinese (Simplified and Traditional), and Malay, including mixed-language documents.
- Embeddings: `bge-m3` (multilingual) so one index serves all three languages.
- Keyword search: each chunk records a detected language. English uses the PostgreSQL `english` configuration; Malay uses `simple`; Chinese is segmented in the application before being indexed with `simple`. Language configuration is part of the index-generation compatibility key.
- OCR: Tesseract `eng`, `chi_sim`, `chi_tra`, and `msa` language packs.
- The user interface is English for the MVP. Answers follow the question language.

### Model profiles

| Profile | Chat model | Embedding model | Use |
|---|---|---|---|
| Recommended (32 GB) | `qwen3:8b` | `bge-m3` (1024 dimensions) | Default pilot profile |
| Compact (16 GB) | `qwen3:4b` (to be benchmarked) | `bge-m3` | Development and small pilots |

Model digests and measured latency for each profile are published in release notes.

### Jobs and retrieval

- Job queue: Redis + Dramatiq is retained. A job's PostgreSQL record and its outbox entry commit together; a dispatcher relays committed entries to Dramatiq, and workers acknowledge only after the job completes successfully.
- Reranking: reciprocal-rank fusion of semantic and keyword results for the MVP. A local cross-encoder is added only if the evaluation report shows a ranking failure it fixes.

### Limits

Owner confirmed all limits on 2026-10-05.

| Limit | Value | Status |
|---|---|---|
| Maximum upload size | 50 MB per file | Decided |
| Maximum pages per document | 2,000 | Decided |
| Maximum decompressed size per file | 500 MB | Decided |
| Website crawl | 500 pages, depth 5, 30 minutes, 10 MB per response | Decided |
| Superseded-version and retired-page retention | 30 days | Decided |
| Conversation retention | 90 days | Decided |
| Corpus scaling review | 100,000 chunks | Decided (§11) |

### Quality-gate cadence

The pull-request gate runs the Playwright smoke projects; the complete device matrix runs nightly and in `make check-release`, which blocks every package (see §8).
