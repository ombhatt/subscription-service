# Explicit interpreter: a bare `python`/`python3` on macOS is often Xcode's 3.9
# (or worse), and this service needs 3.11+. Override if yours lives elsewhere:
#   make install PYTHON=/opt/homebrew/bin/python3.12
PYTHON ?= python3.11

.PHONY: up down services services-down install migrate seed run test testclock lint

up:            ## start postgres + redis in docker
	docker compose up -d

down:
	docker compose down

services:      ## start postgres + redis installed via homebrew instead
	brew services start postgresql@16
	brew services start redis
	./scripts/setup_local_db.sh

services-down:
	brew services stop postgresql@16
	brew services stop redis

install:
	$(PYTHON) -m venv .venv
	.venv/bin/pip install --upgrade pip
	.venv/bin/pip install -r requirements-dev.txt

migrate:
	.venv/bin/alembic upgrade head

seed:          ## create products/prices in Stripe, prints price ids for .env
	.venv/bin/python -m scripts.seed_stripe

run:
	.venv/bin/uvicorn app.main:app --reload --port 8000

testclock:      ## lifecycle against real Stripe sandbox objects (needs .env key + network)
	.venv/bin/pytest integration/ -v

test:
	.venv/bin/pytest -q

lint:
	.venv/bin/ruff check app tests scripts integration
