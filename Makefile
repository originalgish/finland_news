# Dev shortcuts. Run `make install` once to create .venv for lint and tests.
# `make up` starts the database and the web app (http://localhost:5000). The worker is started
# with `make run-worker STAGE=...` and needs no long-running container yet.

COMPOSE ?= docker compose
PY ?= .venv/bin/python

.PHONY: install up down migrate psql test lint css run-worker

install:
	uv venv --python 3.12 .venv
	uv pip install --python $(PY) -e ".[dev]"

up:
	$(COMPOSE) up -d --build db web

down:
	$(COMPOSE) down

migrate:
	$(COMPOSE) run --rm --build web python -m app.migrate

psql:
	$(COMPOSE) exec db psql -U ft -d ft

# Set TEST_DATABASE_URL to run the migration integration test against a real Postgres.
test:
	$(PY) -m pytest

lint:
	$(PY) -m ruff check .

# Needs web/static/input.css, which arrives in phase 6.
css:
	tailwindcss -i web/static/input.css -o web/static/app.css --minify

run-worker:
	@test -n "$(STAGE)" || { echo "usage: make run-worker STAGE=fetch"; exit 1; }
	$(COMPOSE) run --rm --build worker python -m worker $(STAGE)
