# Design Specification: Smart RAG AI Local-First MVP

**Date:** 2026-10-04

**Status:** Approved design; implementation plan not yet approved

**Owner:** Founder/CTO
**Primary audience:** Product owner and implementation engineer

## 1. Objective

Build a local-first RAG web application for SMEs. Owners and administrators upload documents or synchronize website content. Authorized users ask questions and receive evidence-grounded answers with exact citations. Updating a source activates a complete new version and excludes obsolete knowledge; deleting a source removes it from retrieval immediately and purges its stored/derived content through a verified workflow.

The MVP runs on one PC. Ollama provides local generation and embeddings by default. The owner can switch answer generation to the GLM general API without changing code or re-indexing documents. Hosted generation is opt-in and visibly sends the answer policy, current question, selected passage text, citation labels/IDs, and—if enabled—up to four visible conversation messages outside the PC. Optional GLM query rewriting occurs before retrieval and sends only its rewrite policy, the current question, and the same optional conversation-history limit.

Detailed product requirements are in [Product and MVP](../../product-and-mvp.md). UX behavior is in [Application design](../../application-design.md). Architecture and security requirements are in [System architecture](../../system-architecture.md) and [Knowledge lifecycle and security](../../knowledge-lifecycle-and-security.md).

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

### Ingestion

- Supported files are accepted asynchronously and retain original checksum and location metadata.
- Corrupt, encrypted, unsupported, or over-limit content fails safely with a reason.
- A source is not searchable until its complete version passes validation and becomes active.
- Website crawls stay within configured host/path and resource limits.

### Retrieval and answers

- Every retrieval applies workspace, user grant, source state, active-version, and active-index filters.
- Search combines semantic and keyword evidence.
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

## 4. Technology stack

- React + TypeScript + Vite
- FastAPI + Pydantic + SQLAlchemy + Alembic
- PostgreSQL + `pgvector` + PostgreSQL full-text search
- Redis + Dramatiq workers
- Local filesystem storage behind a stable object-storage interface
- Ollama for default local generation and embeddings
- GLM general API adapter for optional hosted generation
- Docling-led parsing adapters with local OCR
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

# Run all automated checks
make check

# Run backend tests
make test-api

# Run frontend tests
make test-web

# Run browser-level tests
make test-e2e

# Format and lint supported source files
make lint

# Create a versioned local backup
make backup

# Display service health and sanitized diagnostics
make doctor
```

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

Evaluation changes are reviewed like code. A model or chunking change does not ship merely because a few examples look better.

### End-to-end tests

- first-run setup;
- upload to cited answer;
- failed update retaining old knowledge;
- successful replacement excluding old knowledge;
- permission change affecting retrieval;
- deletion and verification;
- Ollama-to-GLM generation switch; and
- backup/restore smoke workflow.

## 9. Engineering boundaries

### Always do

- Validate input at every trust boundary.
- Apply workspace, permission, active-version, and deletion filters in retrieval storage queries.
- Make jobs idempotent and observable.
- Preserve the last good source/index until a replacement passes validation.
- Run affected tests and `make check` before merge.
- Update this specification before implementing a changed architectural decision.
- Pin and audit dependencies used to parse untrusted files.

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

## 10. Operational requirements

- Health endpoints distinguish liveness from dependency readiness.
- Structured logs include correlation, job, workspace, source, and version identifiers where safe.
- Diagnostics redact secrets and source content.
- Database and file storage expose disk-capacity warnings before exhaustion.
- Startup performs compatible schema checks and refuses unsafe downgrades.
- Graceful shutdown stops accepting new work and returns in-progress jobs to a recoverable state.
- Restore documentation names supported version compatibility and rollback limits.
- The default deployment binds only to loopback. LAN mode exposes only a TLS reverse proxy and uses explicit host, origin, proxy, cookie, and firewall settings.

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
4. setup and restore are verified on a clean supported machine;
5. security tests cover permissions, crawl boundaries, prompt injection, secrets, and deletion;
6. the owner can deliberately switch generation between Ollama and GLM without re-indexing;
7. known limitations and hardware profiles are documented; and
8. a design partner can complete the core workflow without developer intervention.

## 13. Deferred decisions

The following decisions are intentionally deferred until implementation planning or customer evidence supplies the missing information:

- first officially supported operating systems;
- exact Ollama chat and embedding model profiles;
- GLM model identifier and commercial data-processing terms;
- default file-size, crawl, retention, and corpus limits;
- packaging approach beyond Docker Compose for non-technical customers; and
- target vertical after design-partner discovery.

These are bounded configuration/product decisions, not gaps in the architectural safety model.

## 14. Pre-implementation decision gates

Deferred decisions must be closed before the dependent implementation task begins:

| Gate | Must be decided before |
|---|---|
| Primary supported operating system and CPU architecture | Foundation/container scaffolding |
| Ollama chat and embedding model profiles | AI-provider contract and performance fixtures |
| GLM model, account type, and commercial data-processing review | Live GLM adapter testing |
| File, crawl, corpus, and retention defaults | Public ingestion and settings contracts |
| Packaging beyond Docker Compose | Design-partner onboarding work |
| Initial vertical | Vertical-specific templates, terminology, or marketing work |

The implementation plan must record each decision or explicitly stop before the dependent task. No engineer should infer a default silently.
