# Explicit interpreter: a bare `python`/`python3` on macOS is often Xcode's 3.9
# (or worse), and this service needs 3.11+. Override if yours lives elsewhere:
#   make install PYTHON=/opt/homebrew/bin/python3.12
PYTHON ?= python3.11

.PHONY: up down services services-down install lock migrate seed run test testclock lint \
        image image-web stack stack-down

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

lock:          ## regenerate requirements.lock after editing requirements.txt
	PYTHON=$(PYTHON) ./scripts/lock.sh

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
