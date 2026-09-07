.PHONY: help setup backend frontend dev seed test lint fmt bench docker clean

help:
	@echo "SmartPark — make targets"
	@echo "  make setup     Install backend + frontend dependencies"
	@echo "  make seed      Reset DB and load a realistic demo facility"
	@echo "  make backend   Run FastAPI on :8000"
	@echo "  make frontend  Run Vite dev server on :5173"
	@echo "  make dev       Run both (backend in background)"
	@echo "  make test      Run the backend test suite"
	@echo "  make bench     Run the allocation-strategy benchmark"
	@echo "  make docker    Build and start the full stack via docker compose"

setup:
	cd backend && python3 -m venv .venv && .venv/bin/pip install -U pip && .venv/bin/pip install -r requirements.txt
	cd frontend && npm install

seed:
	cd backend && .venv/bin/python -m scripts.seed --reset

backend:
	cd backend && .venv/bin/uvicorn app.main:app --reload --port 8000

frontend:
	cd frontend && npm run dev

dev:
	cd backend && .venv/bin/uvicorn app.main:app --reload --port 8000 & \
	cd frontend && npm run dev

test:
	cd backend && .venv/bin/pytest -q

lint:
	cd backend && .venv/bin/ruff check app tests

fmt:
	cd backend && .venv/bin/ruff format app tests

bench:
	cd backend && .venv/bin/python -m scripts.benchmark --trials 5 --vehicles 800

docker:
	docker compose up --build

clean:
	rm -rf backend/data/smartpark.db backend/.pytest_cache backend/**/__pycache__
