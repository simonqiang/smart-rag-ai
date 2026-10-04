# Implementation Plan: Smart RAG AI Local-First MVP

## Overview

Build the approved local-first SME knowledge assistant as a sequence of vertically testable slices. Each slice follows red–green–refactor TDD, maintains the 96% per-module coverage gates, and adds Playwright happy/unhappy coverage when it changes a user workflow.

The detailed task instructions are in [the agent execution plan](../docs/superpowers/plans/2026-10-05-smart-rag-mvp-implementation.md). Progress is tracked in [todo.md](./todo.md).

## Feasibility estimate

### Planning assumptions

- One founder-engineer.
- Work happens after normal working hours.
- Baseline capacity: 15 focused engineering hours per week.
- No existing application code is available.
- Scope includes responsive desktop/tablet/mobile UX, multi-user permissions, document and website ingestion, local Ollama, optional GLM, versioning/deletion, backup/restore, >95% coverage, and Playwright E2E.
- Estimate includes a 20% contingency for parser/model/hardware surprises.
- Customer discovery and pilot feedback require calendar time even when engineering is complete.

### Calendar scenarios

| Weekly capacity | Pilot-ready MVP | Stabilization pilot | Local paid MVP |
|---|---:|---:|---:|
| 10 hours/week | 60–70 weeks | 6–8 weeks | 68–80 weeks total |
| 15 hours/week | **40–46 weeks** | 4–6 weeks | **46–54 weeks total** |
| 20 hours/week | 30–35 weeks | 4–6 weeks | 36–43 weeks total |

The feasible commitment is **approximately 42 weeks for a pilot-ready MVP at 15 hours/week**, followed by a **4–6 week real-customer pilot** and up to two weeks of post-pilot release closeout. A reliable paid local product is therefore approximately **11–13 months** from start.

### Faster validation option

A private alpha can be available around week 16–18 by limiting it to one owner, PDF/TXT/Markdown, manual uploads, Ollama, basic cited answers, and one responsive Ask flow. This alpha is for learning only; it does not satisfy the approved MVP definition because full permissions, website sync, deletion verification, broader formats, backup/restore, GLM, and full responsive administration are incomplete.

## Phase schedule at 15 hours/week

| Phase | Weeks | Approx. hours | Outcome |
|---|---:|---:|---|
| 0. Decisions and evaluation baseline | 1–2 | 18 | Supported host/profile decisions and versioned RAG truth set |
| 1. Engineering foundation | 3–7 | 52 | Repository, Compose stack, CI, audit/outbox, storage/jobs |
| 2. Identity and source catalog | 8–11 | 50 | First-run recovery, secure sign-in, roles, collections, responsive shell |
| 3. First end-to-end RAG slice | 12–18 | 116 | Upload PDF/TXT/MD, Ollama index, Ask stream, citations, responsive E2E |
| 4. Knowledge lifecycle guarantees | 19–24 | 64 | Archive, replacement, rollback, deletion verification, index rebuild safety |
| 5. Ingestion breadth and website sync | 25–31 | 68 | DOCX/PPTX/XLSX/CSV/image OCR and lifecycle-safe scheduled crawling |
| 6. Operations and provider choice | 32–36 | 72 | Activity/audit/diagnostics, GLM, backup/restore, feedback |
| 7. Responsive, security, and performance hardening | 37–40 | 52 | Complete device workflows, isolation, abuse, performance, RAG gates |
| 8. Packaging and pilot release | 41–42 | 20 | Installer workflow, release evidence, rollback, clean-machine pilot build |
| Contingency | Distributed across phases | 110–140 | Hardware, extraction, model, security, integration, and after-work interruption |

The bottom-up work-package estimates total approximately **512 engineering hours**. A 25% planning reserve produces an envelope of roughly **640 hours**. At 15 focused hours per week this is about 43 fully productive weeks, supporting the 40–46 week pilot-ready range.

### Work-package estimates

| Tasks | Hours | Notes |
|---|---:|---|
| 1–2 | 18 | Runtime decisions and evaluation baseline |
| 3–5 | 52 | Toolchain, topology, persistence, jobs, audit/outbox, storage ports |
| 6–8 | 50 | First-run/auth, authorization/admin UI, source shell |
| 9–15 | 116 | Upload, providers, extraction, index, retrieval, answers/stream, Ask UI |
| 16–18 | 64 | Archive/versioning, deletion, concurrent rebuild |
| 19–21 | 68 | Business formats/OCR and website snapshot lifecycle |
| 22–25 | 72 | Operations/audit UI, GLM, backup/restore, analytics |
| 26–27 | 52 | Responsive/accessibility, security/performance/RAG hardening |
| 28 | 20 | Packaging and pilot release verification |
| **Total** | **512** | Before 25% planning reserve |

## Dependency graph

```text
Decisions + evaluation fixtures
          ↓
Repository/CI → Compose foundation → identity/access
                                      ↓
                              source catalog/upload
                                      ↓
AI provider contracts → extraction → indexing → retrieval/answers → Ask UI
                                      ↓
                    versioning → deletion → index rebuild
                                      ↓
                     format adapters + website sync
                                      ↓
              operations + GLM + backup + analytics
                                      ↓
              responsive/security/performance hardening
                                      ↓
                         packaging and pilot release
```

## Architecture decisions

- Modular monolith with separate API and worker processes.
- PostgreSQL/`pgvector` is the source of truth and search store for MVP.
- Redis carries jobs but never owns unrecoverable state.
- Local filesystem storage is accessed through an interface suitable for later S3 replacement.
- Ollama is the default local generation/embedding provider.
- GLM uses a separate generation adapter and never becomes an implicit fallback.
- Source and index activation use transactional pointers; concurrent index changes use a transactional outbox.
- Every browser workflow is mobile-first and tested with Playwright device projects.

## Verification checkpoints

### Checkpoint A — Week 7

- `make check` runs locally and in CI.
- Coverage gates fail correctly below 96%.
- Compose services start, persist data, and report health.
- No application feature work proceeds with a broken pipeline.

### Checkpoint B — Week 18

- Owner can sign in, upload PDF/TXT/Markdown, and receive a cited Ollama answer.
- Insufficient-evidence behavior works.
- The flow passes Playwright on mobile, tablet, and desktop.
- Private alpha may begin with synthetic/non-sensitive content.

### Checkpoint C — Week 24

- Failed replacement keeps old knowledge active.
- Successful replacement excludes old knowledge.
- Deletion reaches verified zero state.
- Concurrent index rebuild catches source changes transactionally.

### Checkpoint D — Week 36

- Supported document types and website synchronization pass fixtures.
- Owner can switch Ollama generation to GLM without re-indexing.
- Backup restores a clean installation.
- Operators can diagnose and retry safe failures.

### Checkpoint E — Week 42

- All approved acceptance criteria and quality thresholds pass.
- Responsive role-based E2E suite passes.
- Release evidence and rollback instructions exist.
- Pilot build is ready for real design partners.

## Risks and mitigations

| Risk | Impact | Mitigation |
|---|---|---|
| Local model is too slow or weak on target PC | High | Benchmark in week 1; keep GLM adapter; publish tested profiles |
| Complex files produce poor structure/citations | High | Fixture-led adapters; fail visibly; ship quality levels per format |
| 96% per-module coverage slows early delivery | Medium | TDD from first task; fast unit-heavy pyramid; deterministic provider fakes |
| Website crawler creates security exposure | High | Private-network denial, allowlists, limits, redirect checks, security tests |
| Responsive admin workflows expand UI scope | Medium | Mobile-first components from first screen; device E2E continuously, not at end |
| After-work fatigue reduces throughput | High | Plan on 15 hours, cap weekly work, preserve contingency, keep tasks under one session |
| Pilot feedback changes priorities | Medium | Alpha at week 12–14; defer non-core connectors and cosmetic features |
| Ollama/model ecosystem changes | Medium | Provider contracts and configuration profiles; do not couple domain code to a model |

## Scope controls

The 32-week estimate holds only if these remain deferred:

- native mobile applications;
- public multi-tenant cloud deployment;
- enterprise SSO/SCIM;
- autonomous agents/actions;
- authenticated third-party connectors beyond websites;
- fine-tuning;
- advanced billing; and
- on-premises enterprise orchestration beyond the documented local stack.

Any addition must replace existing scope or move the target date.
