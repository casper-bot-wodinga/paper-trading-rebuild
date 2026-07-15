"""Integration tests for the Docker Compose stack.

These tests require a running Docker Compose stack with the following services:
  - trading-db (Postgres on :5432)
  - data-bus (HTTP on :5000)
  - dashboard (HTTP on :5002)

They are skipped (not failed) when the services are not available, so they
can coexist with unit tests in the same test suite. In CI mode, they fail
hard if services are unreachable.

Run with:
    pytest tests/integration/

Or as part of the full stack:
    docker compose -f docker-compose.test.yml up --build
"""