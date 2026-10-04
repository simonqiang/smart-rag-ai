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
- Scope includes responsive desktop/tablet/mobile UX, multi-user permissions, document and website ingestion, local Ollama, optional GLM, versioning/deletion/retention, backup/restore, >95% coverage, and Playwright E2E.
- Supported platforms are Windows 11 x86_64, macOS 14+ on Apple Silicon, and Ubuntu 22.04/24.04 LTS x86_64.
- Supported content languages are English, Chinese (Simplified/Traditional), and Malay.
- Estimate includes a 25% planning reserve for parser/model/hardware/platform surprises.
- Customer discovery and pilot feedback require calendar time even when engineering is complete.

### Calendar scenarios

| Weekly capacity | Pilot-ready MVP | Stabilization pilot | Local paid MVP |
|---|---:|---:|---:|
| 10 hours/week | 66–76 weeks | 6–8 weeks | 74–86 weeks total |
| 15 hours/week | **44–50 weeks** | 4–6 weeks | **50–58 weeks total** |
| 20 hours/week | 33–38 weeks | 4–6 weeks | 39–46 weeks total |

The feasible commitment is **approximately 46 weeks for a pilot-ready MVP at 15 hours/week**, followed by a **4–6 week real-customer pilot** and up to two weeks of post-pilot release closeout. A reliable paid local product is therefore approximately **12–14 months** from start.

The 2026-10-05 plan review added three supported operating systems, three content languages, HTML upload, retention purge, invitations and owner recovery, and conversation history. These add 44 base engineering hours (about 55 hours including the 25% reserve, or about four weeks at 15 hours/week) to the previous 42-week estimate.

### Faster validation option

A private alpha can be available around week 19–21 by limiting it to one owner, PDF/TXT/Markdown, manual uploads, Ollama, basic cited answers, and one responsive Ask flow on the reference macOS machine. This alpha is for learning only; it does not satisfy the approved MVP definition because full permissions, website sync, deletion verification, broader formats, backup/restore, GLM, other platforms, and full responsive administration are incomplete.

## Customer-discovery track

Customer discovery runs in parallel with engineering and does not wait for code (see [Delivery roadmap](../docs/delivery-roadmap.md) Phase 0). Budget about 2 hours/week, outside the engineering hours above.

| Weeks | Discovery outcome | Feeds |
|---|---|---|
| 1–8 | Interview 10–15 SMEs; confirm the platforms and languages they use; record repeated questions and sensitivity | Task 2 evaluation dataset, decision record |
| 9–16 | Recruit 3–5 design partners; collect 50–100 real questions with expected evidence (stored outside the repository if confidential) | Evaluation set growth, Task 13/14 tuning |
| 17–21 | Run guided walkthroughs of the private alpha with partners | Checkpoint B review, scope cuts |
| 22–46 | Monthly partner check-ins; validate willingness to pay before any billing or cloud work | Phase 6–8 priorities, pilot readiness |

**Discovery gate:** if fewer than three partners commit to testing with real knowledge by week 16, stop and review scope before starting Phase 4.

## Phase schedule at 15 hours/week

| Phase | Weeks | Approx. hours | Outcome |
|---|---:|---:|---|
| 0. Decisions and evaluation baseline | 1–2 | 20 | Pinned platform/model/limit profile and multilingual versioned RAG truth set |
| 1. Engineering foundation | 3–7 | 52 | Repository, Compose stack, CI, audit/outbox, storage/jobs |
| 2. Identity and source catalog | 8–12 | 56 | First-run recovery, secure sign-in, invitations, owner recovery, roles, collections, responsive shell |
| 3. First end-to-end RAG slice | 13–21 | 130 | Upload PDF/TXT/MD, multilingual Ollama index, Ask stream, citations, conversation history, responsive E2E |
| 4. Knowledge lifecycle guarantees | 22–27 | 68 | Archive, replacement, rollback, deletion verification, retention purge, index rebuild safety |
| 5. Ingestion breadth and website sync | 28–34 | 74 | HTML/DOCX/PPTX/XLSX/CSV/image OCR (three languages) and lifecycle-safe scheduled crawling |
| 6. Operations and provider choice | 35–39 | 72 | Activity/audit/diagnostics, GLM, backup/restore, feedback |
| 7. Responsive, security, and performance hardening | 40–43 | 52 | Complete device workflows, isolation, abuse, performance, RAG gates |
| 8. Packaging and pilot release | 44–46 | 32 | Installer workflow on three platforms, release evidence, rollback, clean-machine pilot builds |
| Planning reserve | Distributed across phases | 125–155 | Hardware, extraction, model, platform, security, integration, and after-work interruption |

The bottom-up work-package estimates total approximately **556 engineering hours**. A 25% planning reserve produces an envelope of roughly **695 hours**. At 15 focused hours per week this is about 46 fully productive weeks, supporting the 44–50 week pilot-ready range.

### Work-package estimates

| Tasks | Hours | Notes |
|---|---:|---|
| 1–2 | 20 | Runtime/platform decisions and multilingual evaluation baseline |
| 3–5 | 52 | Toolchain, topology, persistence, jobs, audit/outbox, storage ports |
| 6–8 | 56 | First-run/auth, invitations and owner recovery, authorization/admin UI, source shell |
| 9–15 | 130 | Upload, providers, extraction, multilingual index/retrieval, answers/stream, Ask UI with conversation history |
| 16–18 | 68 | Archive/versioning, deletion and retention purge, concurrent rebuild |
| 19–21 | 74 | HTML/business formats/multilingual OCR and website snapshot lifecycle |
| 22–25 | 72 | Operations/audit UI, GLM, backup/restore, analytics |
| 26–27 | 52 | Responsive/accessibility, security/performance/RAG hardening |
| 28 | 32 | Packaging and clean-machine verification on Windows, macOS, and Linux |
| **Total** | **556** | Before 25% planning reserve |

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
- Redis carries jobs but never owns unrecoverable state; jobs are dispatched from committed PostgreSQL records through the transactional outbox.
- Local filesystem storage is accessed through an interface suitable for later S3 replacement.
- Ollama is the default local generation/embedding provider (`qwen3:8b` + `bge-m3` on the recommended profile).
- GLM uses a separate generation adapter and never becomes an implicit fallback.
- Source and index activation use transactional pointers; concurrent index changes use a transactional outbox.
- Hybrid retrieval uses reciprocal-rank fusion; a cross-encoder is added only when evaluation proves it is needed.
- Keyword search is language-aware (English, Chinese segmentation, Malay); embeddings are multilingual.
- Every browser workflow is mobile-first; pull requests run Playwright smoke projects and the full device matrix runs nightly and before every release.
- The full decision record is in the [canonical specification §15](../docs/superpowers/specs/2026-10-04-smart-rag-local-first-design.md).

## Verification checkpoints

### Checkpoint A — Week 7

- `make check` runs locally and in CI; the nightly full-matrix workflow and `make check-release` exist.
- Coverage gates fail correctly below 96%.
- `make doctor` passes on the reference macOS machine and reports actionable failures for Windows/Linux prerequisites.
- Compose services start, persist data, and report health.
- No application feature work proceeds with a broken pipeline.

### Checkpoint B — Week 21

- Owner can sign in, upload PDF/TXT/Markdown, and receive a cited Ollama answer in English, Chinese, and Malay.
- Insufficient-evidence behavior works.
- The flow passes Playwright on mobile, tablet, and desktop.
- Private alpha may begin with synthetic/non-sensitive content.

### Checkpoint C — Week 27

- Failed replacement keeps old knowledge active.
- Successful replacement excludes old knowledge.
- Deletion reaches verified zero state.
- Retention purge removes expired superseded versions without touching active knowledge.
- Concurrent index rebuild catches source changes transactionally.

### Checkpoint D — Week 39

- Supported document types and website synchronization pass fixtures.
- Owner can switch Ollama generation to GLM without re-indexing.
- Backup restores a clean installation.
- Operators can diagnose and retry safe failures.

### Checkpoint E — Week 46

- All approved acceptance criteria and quality thresholds pass.
- `make check-release` passes, including the complete responsive role-based E2E matrix.
- Clean-machine setup, backup/restore, and upgrade pass on Windows 11, macOS (Apple Silicon), and Ubuntu LTS.
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
| Pilot feedback changes priorities | Medium | Discovery track from week 1; partner walkthroughs of the alpha around weeks 19–21; defer non-core connectors and cosmetic features |
| Chinese/Malay keyword retrieval underperforms | High | Multilingual embeddings; application-side Chinese segmentation chosen by per-language evaluation in Task 13; per-language thresholds block release |
| Windows Docker/WSL2 setup fails for non-technical owners | High | `make doctor` platform remediation from Task 1; nightly Windows install smoke tests; clean-machine verification in Task 28 |
| Docker Desktop licence cost for larger customers | Medium | Document licence terms in the operator guide; evaluate a tested alternative runtime before pilot packaging |
| Parser dependency licence incompatible with distribution | Medium | Licence scan in `make check`; no AGPL parsers (for example PyMuPDF) in shipped code |
| Ollama/model ecosystem changes | Medium | Provider contracts and configuration profiles; do not couple domain code to a model |

## Scope controls

The 46-week estimate holds only if these remain deferred:

- native mobile applications;
- public multi-tenant cloud deployment;
- enterprise SSO/SCIM;
- autonomous agents/actions;
- authenticated third-party connectors beyond websites;
- fine-tuning;
- advanced billing;
- on-premises enterprise orchestration beyond the documented local stack; and
- a translated (non-English) user interface; content in English, Chinese, and Malay is supported, but interface text stays English.

Any addition must replace existing scope or move the target date.
