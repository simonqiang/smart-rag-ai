# Smart RAG AI — Pilot Operator Guide

This guide covers clean-machine installation, daily operation, LAN configuration, backups, safe upgrades, rollback, and troubleshooting for the local-first SME pilot edition of Smart RAG AI.

---

## 1. Supported Platforms & Prerequisites

Smart RAG AI supports three operating system profiles for the MVP (canonical specification §15):

| Platform | Architecture | Container Runtime | Local AI (Ollama) | OCR Engine |
|---|---|---|---|---|
| **Windows 11** (build 22000+) | `x86_64` (`AMD64`) | Docker Desktop with **WSL2** backend | Native Windows app | Tesseract (`eng`, `chi_sim`, `chi_tra`, `msa`) |
| **macOS 14+** | Apple Silicon (`arm64`) | Docker Desktop for Apple Silicon | Native macOS app (Metal GPU acceleration) | Homebrew `tesseract` + `tesseract-lang` |
| **Ubuntu 22.04 / 24.04 LTS** | `x86_64` | Docker Engine + Compose v2 plugin | Native systemd service | `tesseract-ocr` + language packs |

Intel Macs, Windows on ARM, and Linux `arm64` are out of scope for the MVP and are explicitly refused by `make doctor`.

### Docker Desktop Licence Notice (Windows & macOS)

Docker Desktop is free for small businesses (fewer than 250 employees AND less than $10 million in annual revenue), personal use, education, and non-commercial open-source projects. **Larger organizations require a paid Docker Desktop subscription** under Docker's subscription service agreement.

- **Tested free alternative on Linux:** Ubuntu 22.04/24.04 LTS runs native **Docker Engine + Docker Compose v2 plugin** (`docker.io` / `docker-compose-v2`) with no Docker Desktop licence requirement.
- **macOS alternative note:** Colima or OrbStack may be evaluated by operators subject to their own licensing, though Docker Desktop on Apple Silicon is the reference verification runtime for the MVP.

### Per-Platform Prerequisite Setup

#### Windows 11 (`x86_64`)

1. Enable WSL2 in an elevated PowerShell prompt:
   ```powershell
   wsl --install
   ```
2. Install **Docker Desktop for Windows** and enable **"Use the WSL 2 based engine"** in Settings → General.
3. Install **Ollama for Windows** natively from `https://ollama.com` so it can use local CPU/NVIDIA GPU resources on the host.
4. Install **Tesseract OCR** from the UB-Mannheim installer and include `eng`, `chi_sim`, `chi_tra`, and `msa` language data.
5. Run `make` commands inside your **WSL2 Ubuntu** shell or **Git Bash** terminal.

#### macOS 14+ (Apple Silicon `arm64`)

1. Install **Docker Desktop for Mac (Apple Silicon)** and start it.
2. Install **Ollama for macOS** natively from `https://ollama.com`:
   ```bash
   brew install --cask ollama
   ```
3. Install Tesseract with all language packs:
   ```bash
   brew install tesseract tesseract-lang
   ```

#### Ubuntu 22.04 / 24.04 LTS (`x86_64`)

1. Install Docker Engine and the Compose v2 plugin:
   ```bash
   sudo apt-get update
   sudo apt-get install -y docker.io docker-compose-v2
   sudo usermod -aG docker "$USER"
   ```
2. Install native Ollama:
   ```bash
   curl -fsSL https://ollama.com/install.sh | sh
   ```
3. Install Tesseract and the required language packs:
   ```bash
   sudo apt-get install -y tesseract-ocr tesseract-ocr-eng \
       tesseract-ocr-chi-sim tesseract-ocr-chi-tra tesseract-ocr-msa
   ```

### Pulling Required Ollama Models

Before starting the workspace, pull the models for your hardware profile:

```bash
# Recommended profile (32 GB RAM):
ollama pull qwen3:8b
ollama pull bge-m3

# Compact profile (16 GB RAM):
ollama pull qwen3:4b
ollama pull bge-m3
```

---

## 2. Measured Hardware & Model Profiles

Configuration lives in `config/default.yaml` (override at runtime with `SMART_RAG_PROFILE=recommended` or `SMART_RAG_PROFILE=compact`).

| Profile | Target Hardware | Chat Model | Embedding Model | Measured Retrieval p95 (100k chunks) | Typical Generation Latency |
|---|---|---|---|---|---|
| **`recommended`** (default) | 32 GB RAM, Apple Silicon M-series or 8-core x86_64 (optional NVIDIA GPU) | `qwen3:8b` | `bge-m3` (1024d) | < 1.2 s (gate < 2.0 s) | 3–8 s (Apple Silicon / GPU), 8–18 s (CPU) |
| **`compact`** | 16 GB RAM, CPU-only office PC | `qwen3:4b` | `bge-m3` (1024d) | < 1.5 s (gate < 2.0 s) | 6–15 s (CPU) |

- **Non-AI API p95:** < 500 ms under single-office load.
- **Cached web bundle:** < 300 KB gzip, interactive < 2 s.
- **Job status visibility:** < 2 s.
- **Scaling review threshold:** 100,000 chunks per installation.

---

## 3. Standard Operator Commands

All lifecycle operations are exposed through `make` targets that never delete persistent data:

```bash
# 1. Install Python & web dependencies locally
make setup

# 2. Run host prerequisite and connectivity diagnostics (secrets redacted)
make doctor

# 3. Start the complete local stack (PostgreSQL, Redis, API, Worker, Dispatcher, Web)
make up

# 4. Apply database schema migrations
make migrate

# 5. Stop services cleanly without deleting data or Docker volumes
make down

# 6. Create an encrypted, checksummed backup in ./backups
SMART_RAG_BACKUP_KEY="your-strong-passphrase" make backup

# 7. Restore an encrypted backup into a clean installation
SMART_RAG_BACKUP_KEY="your-strong-passphrase" make restore FILE=backups/<filename>.srb

# 8. Recover owner access on the host PC (interactive prompt only, audited)
make reset-owner-password

# 9. Safely upgrade schema with pre-upgrade backup and job drain
SMART_RAG_BACKUP_KEY="your-strong-passphrase" make upgrade

# 10. Export a secret-redacted, passage-free diagnostic support bundle
make support-bundle
```

---

## 4. First-Run Setup & Daily Operations

1. Run `make setup && make doctor` and resolve any `[FAIL]` items using the printed `fix:` remediation.
2. Run `make up && make migrate`.
3. Open `http://127.0.0.1:8080` in your browser.
4. Complete the **First-Run Setup Wizard** to create the workspace and the initial **Owner** account.
5. Create **Collections**, invite **Administrators** or **Members** (single-use expiring links or temporary passwords), and grant collection access.
6. Upload documents (`PDF`, `DOCX`, `PPTX`, `XLSX`, `CSV`, `TXT`, `MD`, `HTML`, `PNG/JPG/TIFF/WEBP`) or register a website schedule under **Sources**.
7. Ask questions in **Ask** (English, Simplified/Traditional Chinese, or Malay) and inspect source citations.

### Optional Hosted Generation (GLM)

- By default, all embeddings and generation run locally on Ollama.
- To enable GLM generation, set `SMART_RAG_GLM_API_KEY` in the environment, open **Settings → AI Provider** as the Owner, test connectivity, review and check the versioned privacy consent notice, and click **Activate**.
- Embeddings remain local (`bge-m3`) so switching generation requires **no re-indexing**.
- If GLM is unreachable or returns an error, the system fails closed with an explicit error and **never silently falls back**.

---

## 5. Network Exposure & LAN TLS Profile

### Default Loopback Mode

By default (`compose.yaml`), Smart RAG AI binds only to `127.0.0.1` (`127.0.0.1:8080` for Web, `127.0.0.1:8000` for API, `127.0.0.1:54329` for PostgreSQL, `127.0.0.1:63899` for Redis). Internal services are not reachable from other machines.

### Office LAN Mode

To expose the workspace to other devices on a trusted office LAN:

1. Provision a TLS certificate trusted by client devices (e.g., an internal office CA or `mkcert`) and place the certificate and private key on the host.
2. Validate and generate the LAN nginx profile via `infra/scripts/install.py`:
   - Binds only to a **private LAN IP** (e.g., `192.168.1.50:8443`); wildcard `0.0.0.0` and public internet IPs are refused.
   - Enforces an explicit `server_name` host allowlist (no `*` wildcards).
   - Enforces an explicit `https://` CORS origin allowlist.
   - Sets `HttpOnly; Secure; SameSite=Strict` on session cookies.
   - Trusts `X-Forwarded-For` only from the configured proxy CIDR (`set_real_ip_from`).
3. Configure the host firewall (Windows Defender Firewall, macOS Application Firewall / `pf`, or Ubuntu `ufw`) to allow inbound TCP `8443` only from the local office subnet, keeping PostgreSQL (`54329`), Redis (`63899`), and direct API (`8000`) closed to the network.

---

## 6. Backup, Safe Upgrade, and Rollback Procedure

### Encrypted Backups & Compatibility

- Every backup (`make backup`) writes an authenticated `SRB1` AES-GCM archive into `./backups` and records its manifest checksum and schema revision (`0015`).
- The recovery passphrase (`SMART_RAG_BACKUP_KEY`) is never stored on disk or in the database; store it in the company password manager.
- Managed backups expire automatically after 30 days (configurable in **Settings → Retention**) and are immediately purged if a contained source is permanently deleted.
- **Restore compatibility:** `make restore FILE=...` verifies the AES-GCM tag, member SHA-256 checksums, and exact Alembic schema revision equality (`0015`), and requires a clean target installation (zero existing users/workspaces).

### Safe Upgrade & Unsafe-Downgrade Refusal

Run `SMART_RAG_BACKUP_KEY="..." make upgrade`:

1. **Schema compatibility check:** verifies that the database revision is equal to or older than the application's expected head (`0015`). If the database is from a newer revision, `upgrade.py` refuses the unsafe downgrade immediately before touching any data.
2. **Graceful job drain:** any in-progress (`running`) background jobs have their leases cleared and return to `pending` so they resume cleanly after restart.
3. **Pre-upgrade backup:** creates a fresh encrypted `.srb` backup before running Alembic migrations.
4. **Migration execution:** applies `alembic upgrade head`.

### Rollback Procedure

Every deployable revision can roll back to the last verified revision:

1. Stop the running containers without deleting data:
   ```bash
   make down
   ```
2. Check out the previous verified git revision / release tag:
   ```bash
   git checkout <last-verified-revision>
   ```
3. If the failed upgrade modified database schema or state, initialize a clean database volume (`docker compose down -v` **only after verifying your `.srb` backup file with `python infra/scripts/backup_restore.py inspect <file>`**), start PostgreSQL and migrate to that revision's head (`docker compose up -d --wait postgres redis && make migrate`), and restore the pre-upgrade backup:
   ```bash
   SMART_RAG_BACKUP_KEY="your-recovery-key" make restore FILE=backups/<pre-upgrade-backup>.srb
   ```
4. Start the stack and verify health:
   ```bash
   make up && make doctor
   ```

---

## 7. Support Bundle & Troubleshooting

When escalating an issue, generate a sanitized diagnostic archive:

```bash
make support-bundle
```

This writes `build/support-bundle.zip` containing:
- `manifest.json` (platform, profile, schema revision, timestamp)
- `doctor.txt` (redacted host and dependency checks)
- `counts.json` (row counts only)
- `jobs.json` (recent job statuses and typed error codes)
- `audit.json` (recent administrative audit events)

All `SMART_RAG_*` secrets and database passwords are automatically replaced with `[redacted]`, and **no document originals, extracted text, chunks, or conversation messages** are ever included.

---

## 8. Known limitations (MVP)

- **Single workspace per installation:** multi-tenant SaaS hosting is out of scope for the MVP.
- **English UI:** content ingestion, OCR, retrieval, and answers support English, Simplified Chinese, Traditional Chinese, and Malay, while application navigation and labels remain in English.
- **Upload and crawl caps:** 50 MB per uploaded file, 2,000 pages per document, 500 MB decompressed size, and 500 pages / depth 5 / 30 minutes / 10 MB per response for website crawls.
- **Unauthenticated public/intranet web crawling only:** login-protected third-party SaaS connectors are deferred past MVP.
- **Local hardware variance:** local answer generation speed depends on host CPU/GPU and RAM; use the `compact` profile (`qwen3:4b`) or consented GLM generation on 16 GB CPU-only machines.
