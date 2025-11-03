SHELL := /bin/bash

.PHONY: replica server fe dev

replica:
	./scripts/run-replica-postgres.sh

server-prepare:
	cd backend && python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt

server:
	cd backend && .venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload

fe:
	cd frontend && pnpm install && pnpm dev

dev:
	$(MAKE) -j2 server fe 