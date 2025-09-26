SHELL := /bin/bash

.PHONY: server fe dev

server:
	cd backend && .venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload

fe:
	cd frontend && pnpm install && pnpm dev

dev:
	$(MAKE) server fe 