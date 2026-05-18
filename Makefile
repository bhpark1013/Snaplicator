SHELL := /bin/bash

.PHONY: replica server fe dev doctor setup

replica:
	./scripts/run-replica-postgres.sh

server-prepare:
	cd backend && python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt

server:
	cd backend && .venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8888 --reload

fe:
	cd frontend && pnpm install && pnpm dev

dev:
	$(MAKE) -j2 server fe 

doctor:
	cd backend && { [ -x .venv/bin/python ] && .venv/bin/python -m app.services.preflight || python3 -m app.services.preflight; }

setup:
	./scripts/setup.sh
