.PHONY: help install db-init db-shell db-reset ingest analyse \
        docker-up docker-down docker-logs

DB_URL ?= $$(grep ^DATABASE_URL .env | cut -d= -f2-)

help:
	@echo "Targets:"
	@echo "  install        venv + pip install"
	@echo "  db-init        create DB + apply schema (uses local Postgres via \$$DATABASE_URL)"
	@echo "  db-shell       psql into the DB"
	@echo "  db-reset       DROP + recreate DB (DESTRUCTIVE)"
	@echo "  ingest P=name  python ingest.py --platform <name>"
	@echo "  analyse P=name python run_analysis.py --platform <name>"
	@echo ""
	@echo "Docker-compose alternative (only if no local Postgres):"
	@echo "  docker-up / docker-down / docker-logs"

install:
	python3 -m venv .venv
	.venv/bin/pip install -r requirements.txt

# --- local Postgres path (recommended) ---
db-init:
	psql "$$(echo $(DB_URL) | sed 's|/card_platform_analysis|/postgres|')" \
	  -c "CREATE DATABASE card_platform_analysis;" || true
	psql "$(DB_URL)" -f schema/init.sql

db-shell:
	psql "$(DB_URL)"

db-reset:
	psql "$$(echo $(DB_URL) | sed 's|/card_platform_analysis|/postgres|')" \
	  -c "DROP DATABASE IF EXISTS card_platform_analysis;"
	$(MAKE) db-init

ingest:
	.venv/bin/python ingest.py --platform $(P)

analyse:
	.venv/bin/python run_analysis.py --platform $(P)

# --- docker-compose path (fallback if no local Postgres) ---
docker-up:
	docker compose up -d postgres
	@echo "Postgres on localhost:5433 (db=card_platform_analysis user=analyst)"

docker-down:
	docker compose down

docker-logs:
	docker compose logs -f postgres
