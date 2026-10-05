# Smart RAG AI — agent map

State of work: `tasks/todo.md` (progress) and `tasks/plan.md` (schedule).
Task details + interfaces: `docs/superpowers/plans/2026-10-05-smart-rag-mvp-implementation.md`.
Closed decisions: spec §15 in `docs/superpowers/specs/`.

## Commands

- `make check` — full PR gate (requires the stack: `make up` once; Compose deps stay running)
- `make check-release` — adds the full Playwright device matrix
- `make doctor` — host diagnostics; `make migrate` — apply schema
- Environments: `uv sync` + `npm install` once; python runs via `uv run`

## Rules

- TDD per task in todo.md; per-module coverage gate is 96%, changed lines 100%.
- Redis never owns unrecoverable state; jobs dispatch only from committed PostgreSQL outbox rows.
- Secrets never in config files or logs; they arrive via `SMART_RAG_*` env vars.
- Update `tasks/todo.md` checkboxes when a task's verify command passes.
