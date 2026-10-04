# Product and MVP Definition

## 1. Product vision

Smart RAG AI is a local-first knowledge assistant for SMEs. A company can upload internal documents or connect website content, then ask questions in natural language and receive answers supported by citations to the current approved knowledge.

The product is not a general chatbot. Its job is to find, explain, and cite company knowledge while refusing to invent an answer when the indexed evidence is insufficient.

## 2. Founder thesis

SMEs accumulate knowledge across policies, manuals, proposals, product sheets, spreadsheets, and websites. Finding the correct answer is slow, and staff frequently rely on obsolete copies. Enterprise knowledge platforms can be too expensive or operationally complex for a small organization.

The initial opportunity is a simple product with four promises:

1. **Install locally:** begin without cloud infrastructure or a DevOps team.
2. **Use existing knowledge:** ingest common business files and website pages.
3. **Trust the answer:** show the source and refuse unsupported claims.
4. **Stay current:** publish new source versions atomically and retire obsolete knowledge.

## 3. Initial customer profile

The MVP targets general SMEs that:

- have roughly 5–100 knowledge users;
- rely on recurring internal, customer, product, policy, or operational questions;
- maintain tens to thousands of documents rather than millions;
- can nominate an owner who controls sources and access;
- can run the application on a designated PC or small office server; and
- accept a local installation before a managed cloud service exists.

Strong early design-partner candidates include professional services, distributors, small manufacturers, property managers, and support teams.

## 4. User roles

| Role | Responsibilities |
|---|---|
| Owner | Configures the workspace, AI provider, retention, users, backups, and destructive operations |
| Administrator | Manages sources, synchronizations, processing failures, collections, and member access |
| Member | Searches and asks questions using the sources and collections they may access |

The MVP supports one company workspace per installation. The data model should retain a `workspace_id` boundary so a later hosted edition can support multiple tenants without redesigning every record.

## 5. Core jobs to be done

- “Help me find the current company answer without searching folders manually.”
- “Show me the source so I can verify the answer.”
- “Replace an old policy without the assistant quoting both versions.”
- “Tell me when website content or document processing is stale or broken.”
- “Delete a source and verify that it can no longer influence answers.”
- “Keep sensitive content on this PC unless I explicitly enable a hosted model.”

## 6. MVP feature scope

### 6.1 Workspace and access

- Create the local workspace during first-run setup.
- Create owner, administrator, and member accounts.
- Sign in using email/username and password.
- Assign sources to collections and grant users access to collections.
- Enforce authorization in retrieval, not only in the user interface.

### 6.2 Document ingestion

- Upload PDF, DOCX, PPTX, XLSX, CSV, TXT, Markdown, HTML, and common image files.
- Process uploads asynchronously so the interface remains responsive.
- Extract text, headings, tables, page or sheet locations, and useful metadata where the source format permits.
- Apply OCR to scanned pages and images.
- Reject unsupported, corrupt, encrypted, or oversized files with an actionable reason.
- Preserve the original file and a checksum for traceability and duplicate detection.

### 6.3 Website ingestion

- Add a starting URL or sitemap URL.
- Restrict a crawl to approved hosts and path rules.
- Respect configured page and depth limits.
- Extract the meaningful page content while excluding navigation and repeated boilerplate where possible.
- Schedule or manually trigger re-synchronization.
- Detect unchanged pages by content checksum and avoid unnecessary re-embedding.
- Mark removed pages as retired during a successful synchronization.

### 6.4 Knowledge management

- Display every source, its type, owner, collection, active version, last successful update, and health.
- Replace a document by uploading a new version.
- Preserve immutable version history for a configurable rollback period.
- Activate a new version only after all processing and validation succeeds.
- Roll back to a retained prior version.
- Let administrators archive a source and owners permanently delete it.
- Show whether deletion is pending, completed, or failed.

### 6.5 Ask and search

- Accept a natural-language question.
- Optionally filter by collection or source.
- Use hybrid semantic and keyword retrieval.
- Rerank candidate passages before generating an answer.
- Generate a concise answer from retrieved evidence only.
- Cite the document/page/section, spreadsheet/sheet/cell range, or webpage URL where available.
- Let the user open the cited passage in context.
- Say that the indexed evidence is insufficient when the retrieval confidence is below the configured threshold.
- Support follow-up questions within a conversation while preventing conversation history from overriding source evidence.

### 6.6 AI provider configuration

- Use Ollama for local generation and embeddings by default.
- Allow the owner to switch generation to the GLM general API through settings or environment configuration.
- Keep embeddings local when only generation switches to GLM.
- Test provider connectivity without saving invalid settings.
- Show a privacy warning before enabling a hosted provider.
- Never silently fail over from local to hosted inference.
- Track the provider and model used for each answer without logging source text unnecessarily.

### 6.7 Operations and diagnostics

- Show job status and progress for uploads, crawls, extraction, embedding, activation, and deletion.
- Retry transient failures with limits and backoff.
- Provide a manual retry for recoverable failures.
- Expose source-health, job-failure, storage, and model-connectivity diagnostics.
- Export and restore a local backup.
- Record security-relevant administrative actions in an audit log.

## 7. Explicitly out of scope for MVP

- Public multi-tenant SaaS hosting
- Native mobile applications; responsive mobile web support remains required
- Autonomous actions in customer systems
- Fine-tuning foundation models
- Complex workflow or agent builders
- Enterprise SSO, SCIM, and directory synchronization
- Real-time collaborative document editing
- Dozens of third-party data connectors
- Guaranteed support for every file format
- Legal, medical, or financial decision automation
- Formal high-availability or service-level guarantees

## 8. Quality attributes

| Attribute | MVP requirement |
|---|---|
| Grounding | Factual claims must trace to retrieved passages |
| Freshness | Only active source versions participate in retrieval |
| Privacy | Local mode sends no source content to external model providers |
| Security | Retrieval always enforces workspace and collection access |
| Recoverability | Backup and restore cover database, files, and required configuration |
| Transparency | Users can see source health, answer citations, and provider mode |
| Portability | AI, embedding, and storage implementations sit behind stable interfaces |
| Operability | A non-developer can identify failed processing and retry it |
| Testability | Every behavior follows TDD; automated coverage exceeds 95%; Playwright verifies happy and unhappy browser paths |
| Responsive UX | Every member and administration workflow works on desktop, tablet/iPad-class, and mobile devices |

## 9. Product success measures

### MVP readiness

- A new owner can install and reach the setup wizard using documented steps.
- A supported document becomes searchable without developer intervention.
- A website can be synchronized and later updated.
- Answers cite exact source locations.
- A replaced source never returns superseded content after activation.
- A deleted source stops appearing in search and answers.
- Generation can switch between Ollama and GLM without re-indexing.
- Local mode operates without paid model APIs.

### Commercial validation

- Recruit 3–5 design-partner SMEs.
- Each partner indexes real working knowledge and uses the product weekly.
- At least 70% of a curated question set produces an acceptable cited answer in the first pilot; targets rise as the evaluation set matures.
- Users report measurable time saved on a recurring knowledge task.
- At least two partners indicate willingness to pay for continued use, support, or a managed edition.

## 10. Commercial packaging hypothesis

Validate pricing before building billing. Plausible offers are:

- **Local Starter:** one installation, limited users, self-supported.
- **Local Business:** more users, scheduled synchronization, backup tooling, and support.
- **Managed Cloud:** future hosted service with tenant isolation and managed operations.

Do not promise unlimited ingestion or AI usage. Even local deployments have hardware constraints, and hosted generation has variable cost.
