.PHONY: help venv install test lint fmt run clean

VENV ?= .venv
PY := $(VENV)/bin/python
PIP := $(VENV)/bin/pip

help:
	@echo "Targets:"
	@echo "  make venv     - create the virtual environment ($(VENV))"
	@echo "  make install  - install confirmed-ctl (editable) + dev deps"
	@echo "  make test     - run the test suite"
	@echo "  make lint     - run ruff lint checks"
	@echo "  make fmt      - auto-fix lint + format with ruff"
	@echo "  make run      - run 'confirmed-ctl status' (needs confirmed-ctl.yml)"
	@echo "  make clean    - remove venv + caches"

venv:
	python3 -m venv $(VENV) || python3 -m virtualenv $(VENV)

install: venv
	$(PIP) install --upgrade pip
	$(PIP) install -e ".[dev]"

test:
	$(VENV)/bin/pytest

lint:
	$(VENV)/bin/ruff check src tests

fmt:
	$(VENV)/bin/ruff check --fix src tests
	$(VENV)/bin/ruff format src tests

run:
	$(VENV)/bin/confirmed-ctl status

clean:
	rm -rf $(VENV) .pytest_cache .ruff_cache src/*.egg-info
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
