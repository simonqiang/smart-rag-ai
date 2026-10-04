# Knowledge Lifecycle, Security, and Privacy

## 1. Objective

The application must answer from the latest successfully processed knowledge, preserve enough history to diagnose or roll back changes, and reliably stop using retired or deleted content.

“Latest” means the newest complete version activated by the system. A newly uploaded or crawled version is not current merely because it arrived most recently.

## 2. Source and version states

### Source state

```text
active | archived | deletion_pending | deleted | deletion_failed
```

### Version state

```text
created → extracting → chunking → embedding → validating → ready → active
                       ↘ failed
active → superseded → purged
```

Only a version in `active` state is eligible for standard retrieval. A source has at most one active version.

Rollback does not move a superseded record back to `active`. It creates a new staged version from the retained immutable artifacts, processes and validates it under current policies, and then activates it normally. This preserves an append-only history and a single activation path.

## 3. Immutable versioning

- Uploaded originals and extracted artifacts are immutable within a version.
- A replacement creates a new `source_version`; it never mutates chunks belonging to the active version.
- Every chunk records its source version, extraction policy version, chunking policy version, checksum, and location metadata.
- Activation changes the source's active-version pointer inside a database transaction.
- Superseded chunks remain physically present only for the configured rollback period and are always excluded by retrieval filters.

This pattern prevents a user from seeing a half-old, half-new source while an update is processing.

## 4. Document update policy

The MVP supports manual replacement. Optional filesystem watching may be added later.

For a replacement:

1. calculate the file checksum;
2. warn if identical to the active version;
3. create and process a staged version;
4. validate extraction, chunk counts, embeddings, and source locations;
5. activate the staged version transactionally;
6. mark the previous version superseded; and
7. schedule purge after retention.

If any step before activation fails, the old version remains active.

## 5. Website freshness policy

Each website source defines:

- allowed hosts;
- allowed and excluded path patterns;
- starting or sitemap URLs;
- maximum pages, depth, duration, and response size;
- synchronization schedule;
- authentication policy, if supported later; and
- behavior for removed pages.

Each successful sync produces a complete snapshot. Page checksums allow unchanged pages to reuse derived content when all compatibility keys match. A page missing from a complete successful snapshot is retired. A page missing because the crawl failed is not assumed deleted.

Reuse is content-addressed. An unchanged page creates a new snapshot-membership record pointing to an immutable extracted-content artifact and compatible chunk/embedding set. Retrieval joins chunks through membership in the active snapshot. It never treats membership in an older snapshot as evidence that content is current. Reference counting prevents a shared artifact from being physically deleted while an active snapshot still uses it.

The source displays `last_successful_sync_at`, `last_attempt_at`, and a health status. A stale warning appears when the configured freshness interval is exceeded.

## 6. Index generations

An index generation is compatible only when these values match:

- embedding provider;
- embedding model;
- vector dimension;
- embedding normalization policy;
- chunking policy version; and
- relevant language configuration.

When compatibility changes:

1. create a new generation;
2. record a database change-sequence watermark;
3. re-index active source versions in the background while every later source activation or deletion is appended to an index-change ledger in the same PostgreSQL transaction as that lifecycle change;
4. replay ledger entries into the candidate generation until it reaches the current watermark;
5. take a short activation lock, replay the final changes, and verify that candidate memberships exactly match current active source versions and deletion tombstones;
6. activate the new generation and watermark in one transaction; and
7. retire the previous generation after rollback retention.

If catch-up or verification fails, the prior generation remains active. Lifecycle writes never need to update two vector generations synchronously; the durable change ledger is the convergence mechanism.

The ledger is a transactional outbox: a lifecycle change cannot commit without its corresponding sequence entry. Consumers acknowledge entries only after applying them idempotently. This invariant prevents crashes from creating changes that a candidate index can never observe.

Changing only the generation/chat model does not create a new index generation.

## 7. Deletion semantics

Deletion is a tracked, idempotent workflow, not a single database call.

Only the owner can permanently delete a source. An administrator may archive it or request owner action. Owner confirmation transactionally sets a deletion tombstone and excludes the source from retrieval. The asynchronous job uses the source ID and stored-object manifest as its authoritative inventory, then deletes:

- original files;
- extracted text and previews;
- chunks and location records;
- embeddings across index generations;
- crawl artifacts and cached responses; and
- conversation citation snapshots; and
- assistant messages whose evidence includes the source, replacing their content with a deletion notice while retaining minimal audit identifiers.

Database records use explicit source/version foreign keys so the worker can enumerate and delete them without text search. Files use manifest keys rather than directory scans. Cache entries include source/version tags and are invalidated; their maximum TTL is also bounded. Retries resume from the manifest and treat an already-missing record or object as success.

Verification records zero counts for every knowledge table, vector generation, manifest object, citation snapshot, and tagged cache namespace. It also records object-deletion receipts and any failed targets. A deletion reaches `verified` only when every count is zero and no failed target remains. Minimal audit metadata may retain source identifier, actor, timestamps, counts, and outcome, but not deleted content.

Managed backup manifests list included source IDs. After a source deletion, the application creates a clean encrypted backup when backups are enabled, then removes every managed backup containing the deleted source before marking deletion verified. External backup copies are outside application control and are explicitly the owner's responsibility.

## 8. Retrieval authorization

Authorization is enforced in the retrieval query using:

- workspace identifier;
- authenticated user identifier;
- collection grants;
- source state;
- active source-version identifier;
- active index-generation identifier; and
- deletion status.

The application must not retrieve broadly and filter unauthorized passages after ranking or generation. That design could disclose sensitive content through model output, timing, diagnostics, or logs.

## 9. Local and hosted inference privacy

### Local mode

- Questions, retrieved passages, and prompts are sent only to the configured local Ollama endpoint.
- The application does not require internet access for inference after models and containers are available.
- Website synchronization still requires network access to the configured sites.

### GLM mode

- Original files remain in local storage.
- The provider payload may contain the system answer policy, current question, selected retrieved passage text, minimal source display labels/citation IDs, and—when follow-up context is enabled—the last four visible conversation messages.
- Query rewriting occurs before retrieval and sends only its rewrite policy, the current question, and the same optional conversation-history limit; it cannot contain retrieved passages or citation metadata. No GLM payload contains original files, credentials, user identifiers, audit events, diagnostics, or feedback records.
- The UI clearly labels answers as hosted inference.
- The owner must acknowledge this exact payload and whether follow-up context is enabled before activating the provider.
- Provider terms, retention, regional processing, and customer contracts must be reviewed before selling this mode for sensitive data.

No automatic fallback may change a request from local to hosted processing.

## 10. Threats and controls

| Threat | MVP control |
|---|---|
| Cross-user data exposure | Database-level workspace and collection filters on every retrieval path |
| Prompt injection in documents/webpages | Treat source text as quoted evidence; fixed system policy; never execute source instructions |
| Malicious website crawl target | Host/path allowlist, private-network blocking, redirect validation, limits, timeouts |
| Archive bomb or oversized upload | File size, decompression, page, time, and memory limits |
| Parser vulnerabilities | Isolated worker process, patched dependencies, least filesystem access |
| Credential leakage | Secret environment/local store, redaction, no credentials in logs or exports |
| Stale knowledge | Atomic activation, explicit active-version filter, freshness monitoring |
| Hallucinated citation | Citation IDs limited to supplied evidence and validated before response |
| Deletion gaps | Immediate retrieval exclusion plus asynchronous purge verification |
| Lost local PC | Password protection, encrypted disk recommendation, encrypted backups |
| Job replay/duplication | Idempotency keys, version checks, transactional state transitions |

## 11. Authentication and secrets

- Store passwords with a modern password hashing scheme such as Argon2id.
- Use secure, HTTP-only, same-site session cookies.
- Protect state-changing requests against cross-site request forgery where applicable.
- Generate application secrets during setup; never ship a universal default.
- Redact API keys in configuration views after saving.
- Restrict provider configuration, permanent deletion, backup, and restore to the owner. Deletion verification is automatic; the owner can inspect its evidence.

For a local installation, recommend full-disk encryption and an operating-system account dedicated to the application host.

## 12. Audit events

Record at minimum:

- sign-in failures and security-sensitive session events;
- user/role/grant changes;
- source creation, activation, rollback, archive, and deletion;
- crawl-policy and schedule changes;
- provider changes and connection-test outcome;
- embedding index activation;
- backup and restore; and
- administrative retry of failed jobs.

Audit logs store actor, action, target identifier, timestamp, result, and safe metadata. They do not store passwords, API keys, complete prompts, or full source passages.

## 13. Backup and restore

A backup contains:

- PostgreSQL logical backup;
- original and derived files required to restore active knowledge;
- non-secret configuration;
- no plaintext provider credentials or session secrets; and
- a manifest with checksums and schema/application versions.

Every managed backup is encrypted with authenticated encryption. Its recovery key is held separately by the owner and is never stored inside the archive. Restore is owner-only, runs into an empty installation, validates the manifest, migrates compatible schemas, verifies file checksums, and performs a retrieval smoke test. A backup is not considered useful until restore has been tested.

The default managed-backup retention is 30 days and can be reduced by the owner. Backups containing a subsequently deleted source are purged by the deletion workflow regardless of normal retention.

## 14. Security acceptance criteria

- An unauthorized user cannot retrieve, cite, preview, or infer the existence of a restricted source.
- Prompt-injection text in a document cannot cause tool execution or bypass answer policy.
- A failed update leaves the prior active version available.
- A deleted source produces zero eligible retrieval results after exclusion begins.
- Logs and exported diagnostics contain no provider keys or full sensitive passages.
- Hosted inference is impossible until the owner deliberately configures and enables it.
- Backup restoration reproduces active sources, permissions, and searchable answers on a clean installation.
