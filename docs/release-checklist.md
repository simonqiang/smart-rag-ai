# Smart RAG AI — Pilot Release Verification Checklist

No pilot package or deployment may be cut unless every blocking gate and clean-machine check in this checklist passes on the exact target git commit (`docs/testing-and-quality-strategy.md` §7–§9, canonical spec §8, §10, §12).

---

## 1. Automated Release Gate (`make check-release`)

Run on the release candidate commit:

```bash
make check-release
make package
```

Verify that all of the following artifacts are produced and report `PASS`:

- [x] `lint` (`ruff check src apps scripts tests`)
- [x] `typecheck` (`mypy src apps` + `npm run typecheck -w apps/web`)
- [x] `test-unit` (backend unit suite)
- [x] `test-api` (FastAPI route & contract suite)
- [x] `test-web` (Vitest frontend component suite)
- [x] `test-integration` (PostgreSQL, `pgvector`, Redis, Dramatiq, parsers, backup/restore, packaging/upgrade)
- [x] `test-security` (11 threat-table controls, isolation, SSRF, prompt injection, secrets, deletion zero-residue)
- [x] `test-evaluation` (`build/evaluation-report.json` passes all 7 blocking RAG metrics across `en`, `zh-Hans`, `zh-Hant`, `ms`, and `mixed` with `leakage_count == 0`, and `below_profile_results.jsonl` is rejected)
- [x] `build` (Python bytecode compilation + Vite production bundle build)
- [x] `test-performance` (300 KB gzip bundle budget, non-AI API p95 < 500 ms, job visibility < 2 s, 100,000-chunk mixed-language retrieval p95 < 2 s, separate retrieval vs. generation latency measurement)
- [x] `test-e2e` and `test-e2e-full` (complete Playwright device matrix across 320 px, 768 px, 1024 px, and 1440 px viewports in portrait and landscape orientations against the production build)
- [x] `coverage` (≥ 96% line and branch coverage per module, 100% diff coverage on changed executable lines)
- [x] `scan-licence` (no AGPL/GPL contaminated runtime dependencies) and `scan-vulnerabilities` (`pip-audit` + `npm audit --omit=dev`)
- [x] `build/evidence-<short-sha>.json` and `build/release/release-manifest-<short-sha>.json` retained for the exact packaged commit

---

## 2. Clean-Machine Verification Matrix

Verify the release manifest across all three supported operating systems (`docs/operator-guide.md` §1):

| Check | Windows 11 `x86_64` (WSL2 + Docker Desktop) | macOS 14+ Apple Silicon (`arm64`) | Ubuntu 22.04 / 24.04 LTS (`x86_64`) |
|---|---|---|---|
| `make setup` installs clean dependencies | Verified (`ci.yml` / `nightly.yml` install-smoke + host run) | Verified | Verified (`ci.yml` / `nightly.yml`) |
| `make doctor` validates runtime, paths, OCR packs (`eng`, `chi_sim`, `chi_tra`, `msa`), and Ollama models (`qwen3:8b`, `bge-m3`) | Verified | Verified | Verified |
| `make up && make migrate` reaches healthy `/health/live` and `/health/ready` | Verified | Verified | Verified |
| `SMART_RAG_BACKUP_KEY=... make backup` creates encrypted `SRB1` archive | Verified | Verified | Verified |
| Clean-target `make restore FILE=...` restores active sources, permissions, and passes retrieval smoke test | Verified | Verified | Verified |
| `make upgrade` drains in-progress jobs, writes pre-upgrade backup, and refuses unsafe schema downgrades | Verified | Verified | Verified |
| `make down` stops containers while preserving all persistent data and volumes | Verified | Verified | Verified |

---

## 3. Rollback Verification Rehearsal

Before shipping to a design partner, rehearse rollback (`docs/operator-guide.md` §6):

1. Trigger `SMART_RAG_BACKUP_KEY=... make upgrade` and confirm the pre-upgrade `.srb` archive exists in `./backups/`.
2. Simulate a failed migration and confirm `upgrade.py` exits with code `2` and prints the exact `make restore FILE=...` rollback command.
3. Restore the pre-upgrade archive into a clean target database and confirm user accounts, collection grants, active sources, and cited answers are intact.
4. Confirm `make support-bundle` produces `build/support-bundle.zip` with zero plaintext secrets or document passages.

---

## 4. Design-Partner Usability Rehearsal (design-partner onboarding)

Confirm a non-developer operator can complete the end-to-end workflow using only `docs/operator-guide.md` without developer intervention:

1. **Install & first-run:** run `make setup`, `make doctor`, `make up`, and complete the browser Setup Wizard to create the Owner account.
2. **Access control:** create a collection, invite a Member, accept the invitation, and verify collection-scoped visibility.
3. **Multilingual ingestion & cited Ask:** upload English, Chinese, and Malay documents, wait for active status, ask a grounded question in each language, and open the source citation in context.
4. **Lifecycle safety:** upload a replacement version, verify the superseded version never appears in answers, and permanently delete a source to verified zero residue.
5. **Operations & recovery:** inspect `/activity` diagnostics, retry a failed job, export a backup from `/settings/backup` or `make backup`, and export `build/support-bundle.zip`.
