# Smart RAG AI — Documentation Index

This documentation defines the product and technical direction for a local-first Retrieval-Augmented Generation (RAG) application for small and medium-sized enterprises (SMEs).

## Product position

Smart RAG AI lets an SME turn its documents and website content into a private, searchable knowledge assistant. It answers questions from current, authorized sources and cites the evidence used. The MVP runs on one PC and does not require cloud hosting.

## Documents

| Document | Purpose |
|---|---|
| [Product and MVP](./product-and-mvp.md) | Customer, problem, value proposition, scope, requirements, and success measures |
| [Application design](./application-design.md) | User roles, screens, workflows, states, and UX rules |
| [System architecture](./system-architecture.md) | Components, boundaries, data flows, deployment, and provider switching |
| [Knowledge lifecycle and security](./knowledge-lifecycle-and-security.md) | Versioning, freshness, deletion, permissions, privacy, backup, and audit behavior |
| [Delivery roadmap](./delivery-roadmap.md) | Founder-focused phases, commercial validation, risks, and milestones |
| [Canonical design specification](./superpowers/specs/2026-10-04-smart-rag-local-first-design.md) | Build contract, commands, project structure, engineering rules, testing, and acceptance criteria |

## Key decisions

- The MVP is a local, single-company installation.
- Ollama is the default AI provider and keeps inference on the PC.
- GLM is an optional generation provider selected through configuration; it uses the general API, not the coding-plan endpoint.
- Embeddings and generation providers are configured independently.
- Replacing a generation model does not require re-indexing. Replacing an embedding model does.
- A newly processed source version becomes searchable only after the complete version passes validation.
- Superseded and deleted content is never eligible for normal retrieval.
- Every factual answer must include source citations or explicitly say that the evidence is insufficient.

## Intended readers

- Founder/product owner deciding what to build and sell
- Engineer implementing the MVP
- Security or IT reviewer evaluating local deployment
- Early SME design partner validating workflows and controls

## Status

This package is the approved product and architecture design for the MVP. It is a living specification: decisions that change during implementation must be updated here before the implementation diverges.
