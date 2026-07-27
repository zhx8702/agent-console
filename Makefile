.PHONY: help install frontend-install venv up up-app up-wxbot down migrate dev-start dev-start-wxbot dev-stop dev-stop-wxbot dev-restart dev-status dev-health dev-logs run-api run-frontend run-inbound-worker run-outbound-worker run-scheduler-worker run-wxbot-bridge-worker run-worker smoke test lint fmt

PY ?= python3
VENV ?= .venv
BIN := $(VENV)/bin

help:
	@echo "Targets: venv install frontend-install up up-app up-wxbot down migrate dev-start dev-start-wxbot dev-stop dev-stop-wxbot dev-restart dev-status dev-health dev-logs run-api run-frontend run-inbound-worker run-outbound-worker run-scheduler-worker run-wxbot-bridge-worker run-worker smoke test lint fmt"

venv:
	$(PY) -m venv $(VENV)
	$(BIN)/pip install -U pip wheel

install: venv
	$(BIN)/pip install -e ".[dev]"

frontend-install:
	cd frontend && npm install

up:
	docker compose up -d postgres redis qdrant

up-app:
	docker compose --profile app up -d --build api inbound-worker outbound-worker scheduler frontend

up-wxbot:
	docker compose --profile wxbot up -d --build wxbot-bridge-worker

down:
	docker compose down

migrate:
	$(BIN)/alembic upgrade head

dev-start:
	scripts/dev-stack.sh start

dev-start-wxbot:
	scripts/dev-stack.sh start wxbot-bridge

dev-stop:
	scripts/dev-stack.sh stop

dev-stop-wxbot:
	scripts/dev-stack.sh stop wxbot-bridge

dev-restart:
	scripts/dev-stack.sh restart

dev-status:
	scripts/dev-stack.sh status

dev-health:
	scripts/dev-stack.sh health

dev-logs:
	scripts/dev-stack.sh logs

run-api:
	$(BIN)/uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload

run-frontend:
	cd frontend && npm run dev

run-inbound-worker:
	$(BIN)/python -m app.workers.inbound_worker

run-outbound-worker:
	$(BIN)/python -m app.workers.outbound_worker

run-scheduler-worker:
	$(BIN)/python -m app.workers.scheduler_worker

run-wxbot-bridge-worker:
	$(BIN)/python -m app.workers.wxbot_bridge_worker

run-worker:
	$(MAKE) run-inbound-worker & \
	$(MAKE) run-outbound-worker & \
	$(MAKE) run-scheduler-worker

smoke:
	$(BIN)/python -m app.ops.smoke

test:
	$(BIN)/pytest

lint:
	$(BIN)/ruff check app tests

fmt:
	$(BIN)/ruff format app tests
	$(BIN)/ruff check --fix app tests
