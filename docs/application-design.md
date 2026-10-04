# Application Design

## 1. Design principles

1. **Trust is visible.** Put citations, freshness, and provider mode near the answer.
2. **Current beats comprehensive.** Exclude incomplete, superseded, archived, and deleted versions by default.
3. **Progress is understandable.** Long-running ingestion must expose state, progress, and recovery actions.
4. **Privacy is an explicit choice.** Switching from Ollama to GLM requires informed owner action.
5. **Administrative complexity stays out of member workflows.** Members primarily ask, inspect citations, and give feedback.
6. **Destructive actions are deliberate.** Explain impact, require confirmation, and show deletion progress.

## 2. Information architecture

```text
Smart RAG AI
├── Ask
│   ├── New conversation
│   ├── Conversation history
│   └── Saved answers (post-MVP candidate)
├── Knowledge
│   ├── Sources
│   ├── Collections
│   ├── Add document
│   └── Add website
├── Activity
│   ├── Processing jobs
│   ├── Synchronization history
│   └── Deletion history
├── Analytics
│   ├── Feedback
│   ├── Unanswered questions
│   └── Source health
└── Settings
    ├── Workspace
    ├── Users and access
    ├── AI providers
    ├── Retention
    ├── Backup and restore
    └── Diagnostics
```

Members see Ask and the knowledge sources they may access. Owners and administrators see operational and configuration areas according to role.

## 3. Primary screens

### 3.1 First-run setup

The wizard collects:

1. workspace name;
2. owner credentials;
3. storage location confirmation;
4. Ollama endpoint and model checks;
5. optional GLM configuration, which may be skipped;
6. first source upload; and
7. first test question.

The setup completes only after the application verifies database access, writable storage, and the chosen local AI models. GLM is optional and must not block setup.

### 3.2 Ask

The Ask screen contains:

- conversation list;
- question input;
- optional collection/source filters;
- provider badge (`Local — Ollama` or `Hosted — GLM`);
- streamed answer;
- inline citation markers;
- source panel with matching passages;
- helpful/not-helpful feedback; and
- a visible insufficient-evidence state.

An answer citation opens the referenced passage with the query terms highlighted. The interface distinguishes direct evidence from explanatory model text.

The conversation list lets a user reopen, rename, and delete their own conversations. Each conversation shows when it will expire under the retention policy. Reopened conversations re-authorize every citation when it is opened.

The answer is written in the question's language. Citations quote the source passage in its original language, so a Chinese source cited for an English question shows the Chinese passage.

### 3.2a Users and access

Owners and administrators can:

- invite a member with a single-use, expiring link or temporary password and choose role and collections;
- see pending, accepted, and expired invitations and revoke them;
- reset another user's password (owners can reset administrators; administrators can reset members); and
- disable a user, which revokes their sessions immediately.

The owner's own lost password is recovered on the host PC with `make reset-owner-password`; the sign-in screen explains this instead of offering an email reset.

### 3.2b Retention settings

The owner sees and may shorten the retention periods for superseded versions, retired website pages, conversations, and managed backups. The screen shows the next scheduled purge and the counts purged by the last run.

### 3.3 Sources

The source table includes:

- name and source type;
- collection;
- health/status;
- active version;
- last successful update;
- next scheduled synchronization;
- document/page count;
- owner; and
- actions appropriate to the user's role.

Filters include status, collection, source type, and last-update range.

### 3.4 Source detail

The detail screen contains:

- source metadata and permissions;
- current version and prior retained versions;
- ingestion or synchronization history;
- extracted content preview;
- processing warnings;
- version replacement or sync action;
- rollback action; and
- archive/delete action.

The current searchable version is unambiguous. A failed draft version must not look active.

### 3.5 Activity

Every long-running operation uses a consistent state model:

```text
queued → running → validating → completed
                ↘ retrying
                ↘ failed
```

Activation adds a terminal `active` state. Deletion uses `requested → deleting → verified` or `failed`.

Each job shows progress, timestamps, source, attempt count, summary, and a sanitized diagnostic message. Administrators can retry only when the operation is safe to repeat.

### 3.6 AI provider settings

The owner can configure generation and embedding providers separately.

Generation settings:

- provider: Ollama or GLM;
- endpoint;
- model identifier;
- timeout and maximum answer size;
- GLM credential when selected; and
- connectivity test.

Embedding settings:

- provider and endpoint;
- model identifier;
- vector dimension detected by a test call; and
- index migration action when the model changes.

Changing generation takes effect for new questions after a successful test. Changing embeddings creates a new index generation and starts a controlled re-index; the old index remains active until the new one is complete.

## 4. Critical workflows

### 4.1 Upload a document

1. Administrator selects files, collection, access, and optional labels.
2. Client validates extension and configured size limit.
3. API stores the original, calculates its checksum, and creates a source version.
4. Worker extracts and normalizes content.
5. Worker chunks, embeds, and validates the version.
6. The complete version becomes active atomically.
7. The interface reports completion and suggests a test question.

Duplicate content produces a warning and lets the administrator cancel or keep it as a distinct source.

### 4.2 Replace a document

1. Administrator uploads a replacement from the source detail screen.
2. The old version remains active during processing.
3. The new version is staged and validated independently.
4. Activation changes the source's active-version pointer in one transaction.
5. Existing conversations retain historical citations only while the user remains authorized and the retained version still exists. Citation access is re-authorized when opened. Superseded citations are labeled historical; restricted or purged citations show no preview. Permanent source deletion redacts assistant messages supported by that source.

### 4.3 Synchronize a website

1. Scheduler or administrator starts a synchronization.
2. Crawler applies host/path policy and crawl limits.
3. Content hashes identify new, changed, unchanged, and removed pages.
4. Only new or changed content is processed.
5. A complete site snapshot is validated.
6. The snapshot becomes active atomically; removed pages become retired.

If the crawl fails before validation, the last successful snapshot remains active.

### 4.4 Ask a question

1. API authenticates the user and resolves accessible collections.
2. Query service rewrites the question only when configured and retains the original.
3. Retrieval searches active, authorized chunks using semantic and keyword signals.
4. Reciprocal-rank fusion merges semantic and keyword results, selects the strongest evidence, and applies confidence thresholds.
5. Generation provider receives the question, evidence, and answer policy.
6. Citation validator confirms that cited identifiers exist in the evidence set.
7. Answer, citations, latency, provider/model metadata, and feedback hooks are returned.

If no evidence clears the threshold, the system does not call the generator by default; it returns an insufficient-evidence response with possible search suggestions.

### 4.5 Delete a source

1. Administrator may archive a source; only the owner can review the permanent-deletion impact and confirm it.
2. Source is immediately excluded from new retrieval.
3. A deletion job removes derived chunks, vectors, extracted artifacts, and original files.
4. Affected answer text and citation snapshots are redacted while minimal audit metadata is retained.
5. Manifest-based verification confirms that database records, vectors, files, caches, and managed backups contain no source content.
6. UI marks deletion verified or surfaces the failure for retry.

## 5. Error and empty states

- Never show a generic failure when an actionable category is known.
- Distinguish provider unavailable, credential invalid, model missing, extraction unsupported, OCR failed, storage full, crawl blocked, and validation failed.
- Keep the last active source version searchable after failed update processing.
- Do not include raw source content, credentials, or full model prompts in user-visible diagnostics.
- Explain the consequence of empty states and provide one primary next action.

## 6. Accessibility and responsive device support

- Meet WCAG 2.1 AA for keyboard navigation, focus, contrast, labels, and status announcements.
- Render Chinese and Malay content correctly (fonts, line breaking, and `lang` attributes on passages) even though the interface text is English for the MVP.
- Do not encode source health or processing state using color alone.
- Support the complete application on desktop, tablet/iPad-class, and mobile devices; mobile support is not limited to the Ask screen.
- Use mobile-first responsive layouts verified at minimum at 320 px, 768 px, 1024 px, and 1440 px viewport widths.
- Support tablet and mobile portrait and landscape orientations without losing data or blocking an action.
- Adapt navigation, forms, tables, dialogs, citation panels, source management, activity, analytics, and settings to the available width.
- Avoid hover-only interactions; every action must work with touch, keyboard, and pointer input.
- Use touch targets of at least 44 by 44 CSS pixels and prevent unintended page-level horizontal scrolling.
- Keep citations and provider/privacy state accessible to screen readers.

## 7. UX acceptance scenarios

- A first-time owner completes setup without editing configuration files.
- An owner who forgot their password recovers access on the host PC without losing data.
- A member asks in Chinese, English, or Malay and receives an answer in that language with correctly cited source passages.
- A member can distinguish a cited answer from an unsupported response.
- An administrator can identify why a file failed and retry it.
- Owners, administrators, and members can complete their permitted workflows on desktop, tablet, and mobile viewports.
- A user cannot discover a source through search, citations, or autocomplete without permission.
- Before enabling GLM, the owner sees separate disclosures: generation sends the answer policy, current question, selected passage text, citation labels/IDs, and optionally up to four visible conversation messages; pre-retrieval query rewriting sends only its rewrite policy, the current question, and the same optional history limit.
- A failed update never makes the current source disappear.
