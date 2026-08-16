.PHONY: up down logs test lint frontend-build preflight migrate
up:
	docker compose up --build -d
down:
	docker compose down
logs:
	docker compose logs -f --tail=200
test:
	cd backend && PYTHONPATH=. pytest -q
lint:
	cd backend && ruff check app tests --select E9,F63,F7,F82
frontend-build:
	cd frontend && npm ci && npm run build
preflight:
	python scripts/release_preflight.py
migrate:
	docker compose run --rm migrate
