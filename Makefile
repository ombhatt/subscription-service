# Explicit interpreter: a bare `python`/`python3` on macOS is often Xcode's 3.9
# (or worse), and this service needs 3.11+. Override if yours lives elsewhere:
#   make install PYTHON=/opt/homebrew/bin/python3.12
PYTHON ?= python3.11

.PHONY: up down services services-down install lock migrate seed portal run test testclock lint \
        coverage coverage-html audit image image-web stack stack-down api-types

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

install:       ## .venv exactly as uv.lock says, dev tools included (needs uv)
	uv sync --frozen --python $(PYTHON)

lock:          ## re-resolve uv.lock after editing dependencies in pyproject.toml
	uv lock

migrate:
	.venv/bin/alembic upgrade head

seed:          ## create products/prices in Stripe, prints price ids for .env
	.venv/bin/python -m scripts.seed_stripe

portal:        ## configure Stripe's customer portal from plans.py, prints its id
	.venv/bin/python -m scripts.configure_portal

run:
	.venv/bin/uvicorn app.main:app --reload --port 8000

testclock:      ## lifecycle against real Stripe sandbox objects (needs .env key + network)
	.venv/bin/pytest integration/ -v

test:
	.venv/bin/pytest -q

coverage:      ## run the suite under coverage and print the report
	.venv/bin/coverage run -m pytest -q
	.venv/bin/coverage report

coverage-html: ## same, but browsable -- htmlcov/index.html
	.venv/bin/coverage run -m pytest -q
	.venv/bin/coverage html
	@echo "open htmlcov/index.html"

api-types:     ## regenerate the web app's API types after changing a response model
	.venv/bin/python -m scripts.export_openapi
	cd web && npm run gen:api

lint:
	.venv/bin/ruff check app alembic tests scripts integration

# pip-audit runs through uvx, so its ~28 packages never enter .venv. It audits
# the runtime tree as uv.lock pins it, dev tools excluded, the same as CI.
audit:         ## known vulnerabilities in what actually ships
	@echo "--- python (uv.lock, runtime) ---"
	@uv export --frozen --no-dev --no-emit-project --format requirements-txt -o /tmp/runtime-requirements.txt -q
	@uvx pip-audit -r /tmp/runtime-requirements.txt --require-hashes --disable-pip --progress-spinner=off || true
	@echo "--- javascript (web) ---"
	@cd web && npm audit --audit-level=high || true

image:         ## build the API image (also runs the migrations and both jobs)
	docker build -t subscription-api:local .

image-web:     ## build the frontend image; NEXT_PUBLIC_* are baked in at build time
	docker build -t subscription-web:local ./web \
	  --build-arg NEXT_PUBLIC_SUPABASE_URL="$${NEXT_PUBLIC_SUPABASE_URL}" \
	  --build-arg NEXT_PUBLIC_SUPABASE_ANON_KEY="$${NEXT_PUBLIC_SUPABASE_ANON_KEY}"

stack:         ## the whole thing in containers, the way it deploys
	docker compose --profile full up --build

stack-down:
	docker compose --profile full down
