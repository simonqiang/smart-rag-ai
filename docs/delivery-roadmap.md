# Founder Delivery and Commercial Roadmap

## 1. Delivery strategy

Build thin, testable product slices and put them in front of design partners early. The primary founder risk is not whether RAG can be implemented; it is whether a repeatable SME problem is painful enough to pay for and whether the product can deliver trustworthy answers on messy real data.

Avoid cloud platform work, billing, and a wide connector catalog until pilots demonstrate recurring use.

## 2. Phase 0 — Customer discovery and evaluation set

**Outcome:** a narrow pilot problem and a representative truth set.

- Interview 10–15 SMEs across promising segments.
- Identify repeated questions, current search process, data sensitivity, and update frequency.
- Select 3–5 design partners with accessible sample content.
- Collect 50–100 representative questions with expected source evidence, in the languages partners actually use.
- Include positive, negative, ambiguous, stale-version, and permission-denied cases and calculate every blocking RAG evaluation metric.
- Define unacceptable failure cases, especially privacy and obsolete answers.
- Validate the decided platforms (Windows 11, macOS Apple Silicon, Ubuntu LTS) and content languages (English, Chinese, Malay) against partner hardware and content; record any partner that falls outside them.

This discovery work runs as a parallel, non-engineering track alongside engineering Phases 0–3 of the [implementation plan](../tasks/plan.md). It does not wait for the walking skeleton, and its findings feed the evaluation dataset and scope decisions.

**Exit gate:** at least three partners agree to test using real knowledge, and the questions cluster around a repeatable problem.

## 3. Phase 1 — Walking skeleton

**Outcome:** one local end-to-end path.

- Docker Compose development environment.
- Blocking quality pipeline with TDD commands, unit/integration coverage gates, and Playwright.
- First-run owner setup.
- Minimal member accounts, roles, collections, and retrieval grants.
- PDF/TXT/Markdown upload.
- Extraction, chunking, Ollama embedding, and `pgvector` indexing.
- Ask flow with Ollama generation and citations.
- Basic job status.

**Exit gate:** a clean machine can ingest a small document set and answer a curated question with a valid citation; all quality gates pass with greater-than-95% coverage.

## 4. Phase 2 — Trustworthy knowledge lifecycle

**Outcome:** updates and deletion are safe.

- Immutable source versions and atomic activation.
- Replacement workflow and rollback retention.
- Active-version retrieval filter.
- Tracked deletion with verification.
- Index generations for embedding changes.
- Backup and restore baseline.

**Exit gate:** automated tests prove that staged, superseded, unauthorized, and deleted content cannot enter answers.

## 5. Phase 3 — Useful ingestion breadth

**Outcome:** the MVP handles typical SME content.

- DOCX, PPTX, XLSX/CSV, HTML, and image/OCR adapters.
- Extraction previews and warnings.
- Website and sitemap ingestion.
- Change detection and scheduled synchronization.
- Crawl safety controls.

**Exit gate:** each supported format has representative fixtures, location-aware citations, and failure handling.

## 6. Phase 4 — Operational product

**Outcome:** a non-developer can operate a pilot.

- Administration screens for users, roles, collections, and grants established in the walking skeleton.
- Activity, retry, source health, and diagnostics screens.
- GLM general API generation adapter and privacy consent.
- Provider health checks and configuration UI.
- Feedback and unanswered-question reporting.
- Installer/setup documentation and tested restore procedure.

**Exit gate:** a design partner can install, ingest, update, ask, diagnose a failure, and restore from backup without developer intervention.

## 7. Phase 5 — Pilot and commercial validation

**Outcome:** evidence for what to sell.

- Run 4–8 week pilots with 3–5 SMEs.
- Review failed and weak answers weekly against the evaluation set.
- Measure recurring use, accepted answers, time saved, and source maintenance burden.
- Track hardware/performance constraints by installation.
- Test pricing and support expectations.
- Narrow the strongest vertical or use case based on evidence.

**Exit gate:** at least two customers show willingness to pay and the team can describe a repeatable onboarding and value story.

## 8. Phase 6 — Commercial hardening

Choose based on pilot evidence:

### Local commercial edition

- signed installers and automated upgrades;
- license management;
- stronger backup/encryption UX;
- support bundle export;
- compatibility-tested hardware/model profiles; and
- documented data-processing responsibilities.

### Managed cloud edition

- tenant provisioning and isolation;
- managed secrets and key rotation;
- object storage and managed database;
- centralized jobs and observability;
- billing, quotas, and rate limits;
- security review, incident response, and disaster recovery; and
- legal/privacy terms and regional hosting decisions.

Do not attempt both editions simultaneously with a small after-work team.

## 9. Prioritization rule

Prioritize work in this order:

1. prevent data exposure or obsolete answers;
2. make core answers measurably better;
3. make source operations recoverable;
4. reduce onboarding effort;
5. broaden formats/connectors based on customer evidence;
6. improve convenience and visual polish.

## 10. Key risks and mitigations

| Risk | Mitigation |
|---|---|
| Local model quality disappoints | Maintain an evaluation set; offer GLM generation; improve retrieval before adding model complexity |
| Customer hardware is too slow | Publish hardware profiles; benchmark during onboarding; allow hosted generation |
| “Upload anything” creates endless parser scope | Publish supported formats and quality levels; use adapters; reject safely |
| Answers cite stale content | Immutable versions, atomic activation, mandatory active-version filtering |
| Website sync causes SSRF or runaway crawl | Strict allowlists, network blocking, redirect checks, quotas, timeouts |
| Sensitive text leaves the PC unexpectedly | Local default, explicit GLM consent, visible provider badge, no silent failover |
| After-work capacity slows delivery | Modular monolith, narrow MVP, fixed weekly scope, design-partner-driven priorities |
| Free cloud assumptions fail | Treat cloud free tiers as demos only; keep local deployment supported |
| Product is technically sound but not purchased | Discovery and willingness-to-pay gates before billing/cloud investment |

## 11. Founder operating metrics

Track a small dashboard:

- weekly active users per pilot;
- questions per active user;
- answer helpful rate;
- citation-open rate;
- insufficient-evidence rate;
- top unanswered topics;
- source processing success rate;
- median ingestion time and answer latency by hardware/provider;
- stale sources and failed synchronizations;
- time from installation to first cited answer; and
- pilot conversion/willingness to pay.

Metrics must exclude raw source passages and sensitive question text unless a customer explicitly opts into diagnostic collection.

## 12. Near-term non-goals

Do not build these merely because competitors have them:

- agent marketplaces;
- automated outbound actions;
- dozens of integrations;
- custom model training;
- complicated knowledge graphs;
- per-token billing machinery;
- enterprise procurement features; or
- global cloud scale.

They become candidates only when validated customer demand outweighs their support and security cost.
