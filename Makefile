UV ?= uv
PYTHON ?= $(UV) run python3
PYTEST ?= $(UV) run pytest
NPM ?= npm

SMOKE_PROJECTS := smoke-mobile-portrait smoke-tablet-landscape smoke-desktop
GATES := lint typecheck test-unit test-api test-web test-integration test-security test-evaluation build test-performance test-e2e coverage scan

.PHONY: setup doctor test-doctor test-evaluation lint typecheck test-unit test-api test-web \
	test-integration test-security test-performance test-e2e test-e2e-full coverage build scan \
	check check-release $(GATES)

setup:
	$(UV) sync
	$(NPM) install

up:
	docker compose up -d --build --scale worker=1 --scale dispatcher=1

down:
	docker compose down

# Background job processing races database-backed tests (a committed upload
# gets processed twice: once by the test, once by the live pipeline), so the
# pytest gates run with the worker and dispatcher scaled out.
quiet-stack:
	@docker compose stop worker dispatcher 2>/dev/null || true

live-stack:
	@docker compose start worker dispatcher 2>/dev/null || true

migrate:
	$(UV) run alembic upgrade head

doctor:
	PYTHONPATH=src $(PYTHON) -m foundation.doctor

reset-owner-password:
	$(PYTHON) infra/scripts/reset_owner_password.py

test-doctor:
	$(PYTEST) tests/unit/foundation -v

test-evaluation:
	$(PYTEST) tests/evaluation -v
	PYTHONPATH=src $(PYTHON) -m retrieval_answering.evaluation \
		--baseline fixtures/evaluation/baseline.jsonl \
		--results fixtures/evaluation/baseline_results.jsonl \
		--output build/evaluation-report.json
	@! PYTHONPATH=src $(PYTHON) -m retrieval_answering.evaluation \
		--baseline fixtures/evaluation/baseline.jsonl \
		--results fixtures/evaluation/below_profile_results.jsonl \
		--output build/below-profile-report.json

lint:
	$(UV) run ruff check src apps scripts tests

typecheck: typecheck-python typecheck-web

typecheck-python:
	$(UV) run mypy src apps

typecheck-web:
	$(NPM) run typecheck -w apps/web

test-unit: quiet-stack
	$(PYTEST) tests/unit -q

test-api: quiet-stack
	$(PYTEST) tests/api -q

test-web:
	$(NPM) run test -w apps/web

test-integration: quiet-stack
	$(PYTEST) tests/integration -q

test-security: quiet-stack
	$(PYTEST) tests/security -q

test-performance:
	$(PYTHON) scripts/perf_budget.py

test-e2e:
	cd apps/web && npx playwright test $(addprefix --project ,$(SMOKE_PROJECTS))

test-e2e-full:
	cd apps/web && npx playwright test

coverage: quiet-stack
	$(PYTEST) tests --cov=src --cov=apps --cov-branch --cov-report= -q
	$(UV) run coverage xml -o build/coverage.xml
	$(UV) run coverage json -o build/coverage.json
	$(PYTHON) scripts/check_coverage.py build/coverage.json
	$(UV) run diff-cover build/coverage.xml --compare-branch=origin/main --fail-under=100

build:
	$(PYTHON) -m compileall -q src apps
	$(NPM) run build -w apps/web

scan: scan-licence scan-vulnerabilities

scan-licence:
	$(PYTHON) scripts/licence_check.py

scan-vulnerabilities:
	$(UV) run pip-audit --progress-spinner off
	$(NPM) audit --omit=dev

check: $(GATES)
	$(PYTHON) scripts/evidence.py $(GATES)

check-release: check test-e2e-full
	$(PYTHON) scripts/evidence.py check test-e2e-full
