# ═══════════════════════════════════════════════════════════════════════════════
# Makefile — Paper Trading Rebuild
#
# Targets:
#   test              Run full test suite (unit + integration + E2E in Docker)
#   test-unit         Run unit tests only (no Docker needed)
#   test-integration  Run integration tests in Docker Compose
#   test-e2e          Run E2E Playwright tests (needs Docker stack running)
#   up                Start test stack in background
#   down              Tear down test stack
#   clean             Remove all test artifacts
#   logs              Show Docker service logs
#
# Usage:
#   make test         # Full CI-compatible test run
#   make test-unit    # Fast unit tests, no Docker
#   make up && make test-e2e && make down
# ═══════════════════════════════════════════════════════════════════════════════

.PHONY: test test-unit test-integration test-e2e up down clean logs

# ── Config ───────────────────────────────────────────────────────────────────
COMPOSE_FILE ?= docker-compose.test.yml
COMPOSE_OPTS ?=
PYTEST_OPTS ?= -v --tb=short
PYTEST_JUNIT ?= --junitxml=logs/test-results/junit.xml

# ── Full test suite ─────────────────────────────────────────────────────────
# This is the CI-equivalent target. Builds + starts the Docker stack, runs
# all tests (unit, integration, E2E), and tears down. Exit code propagates.
test:
	docker compose -f $(COMPOSE_FILE) $(COMPOSE_OPTS) up --build --abort-on-container-exit --exit-code-from test-runner
	docker compose -f $(COMPOSE_FILE) $(COMPOSE_OPTS) down -v --remove-orphans

# ── Unit tests only (fast, no Docker) ──────────────────────────────────────
# These tests use mocked database connections and don't need any services.
test-unit:
	@echo "═══ Running unit tests only ═══"
	mkdir -p logs/test-results
	PYTHONPATH=. python3 -m pytest tests/ \
		-m 'not integration and not e2e' \
		$(PYTEST_OPTS) \
		$(PYTEST_JUNIT) \
		--junit-prefix=unit \
		-o junit_suite_name=unit
	@echo "═══ Unit tests complete ═══"

# ── Integration tests in Docker ────────────────────────────────────────────
# Starts the full test stack, runs integration + E2E, tears down.
test-integration:
	@echo "═══ Running integration + E2E tests ═══"
	docker compose -f $(COMPOSE_FILE) $(COMPOSE_OPTS) up --build --abort-on-container-exit --exit-code-from test-runner
	docker compose -f $(COMPOSE_FILE) $(COMPOSE_OPTS) down -v --remove-orphans
	@echo "═══ Integration tests complete ═══"

# ── E2E Playwright tests only ──────────────────────────────────────────────
# Runs from the host machine against the running Docker stack.
# Requires Docker stack to be up (make up).
test-e2e:
	@echo "═══ Running E2E Playwright tests ═══"
	mkdir -p test-results
	DASHBOARD_URL=http://localhost:5002 \
	DATA_BUS_URL=http://localhost:5000 \
	npx playwright test --reporter=html,list
	@echo "═══ E2E tests complete ═══"

# ── Start/stop stack ───────────────────────────────────────────────────────
up:
	docker compose -f $(COMPOSE_FILE) $(COMPOSE_OPTS) up --build -d
	@echo "Services starting... (check: docker compose -f $(COMPOSE_FILE) ps)"
	@echo "  Dashboard: http://localhost:5002"
	@echo "  Data-bus:  http://localhost:5000"

down:
	docker compose -f $(COMPOSE_FILE) $(COMPOSE_OPTS) down -v --remove-orphans
	@echo "Stack torn down."

# ── Debug ──────────────────────────────────────────────────────────────────
logs:
	docker compose -f $(COMPOSE_FILE) $(COMPOSE_OPTS) logs --tail=100

# ── Cleanup ────────────────────────────────────────────────────────────────
clean:
	rm -rf logs/test-results/
	rm -rf test-results/
	rm -rf playwright-report/
	docker compose -f $(COMPOSE_FILE) down -v --remove-orphans 2>/dev/null || true
	@echo "Clean complete."

# ── Help ────────────────────────────────────────────────────────────────────
help:
	@echo "Paper Trading Rebuild — Test Targets"
	@echo "──────────────────────────────────────"
	@echo "make test              Full CI pipeline (Docker)"
	@echo "make test-unit         Unit tests only (no Docker)"
	@echo "make test-integration  Integration + E2E in Docker"
	@echo "make test-e2e          Playwright E2E (needs Docker up)"
	@echo "make up                Start test stack in background"
	@echo "make down              Tear down test stack"
	@echo "make logs              Show service logs"
	@echo "make clean             Remove artifacts + tear down"