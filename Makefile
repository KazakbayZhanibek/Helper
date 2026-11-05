PYTHON ?= .venv/bin/python
NPM ?= npm

.PHONY: help prepare dev check check-plan check-backend check-frontend build

help:
	@echo 'prepare       Apply local migrations and seed the synthetic demo (.env required)'
	@echo 'dev           Prepare and run Django + Vite + processing worker; Ctrl+C stops all'
	@echo 'check         Run plan checks, backend tests and frontend production build'
	@echo 'check-plan    Validate the original planning artifacts'
	@echo 'check-backend Check Django, migration drift and backend integration tests'
	@echo 'build         Typecheck and build the frontend'

prepare:
	$(PYTHON) scripts/dev.py --prepare-only

dev:
	$(PYTHON) scripts/dev.py

check: check-plan check-backend check-frontend build

check-plan:
	$(PYTHON) scripts/check_plan.py

check-backend:
	$(PYTHON) backend/manage.py check
	$(PYTHON) backend/manage.py makemigrations --check --dry-run
	$(PYTHON) -m pytest -q

build:
	$(NPM) --prefix frontend run build

check-frontend:
	$(NPM) --prefix frontend test
