.PHONY: install test lint typecheck fmt check

# Every tool runs through the project venv's interpreter when .venv exists —
# a bare tool name resolves through PATH and can land in a sibling env whose
# site-packages shadow this tree (src layout), silently validating a stale
# install. Fallback to PATH's python only when there is no .venv (e.g. CI,
# which installs into its runner env and never had two candidates).
PYTHON ?= $(if $(wildcard .venv/bin/python),.venv/bin/python,python)

## install: editable install with dev tools (into .venv when present)
install:
	$(PYTHON) -m pip install -e ".[dev]"

## test: run the test suite
test:
	$(PYTHON) -m pytest

## lint: static checks without modifying files (matches CI: format + lint)
lint:
	$(PYTHON) -m ruff format --check src tests
	$(PYTHON) -m ruff check src tests

## typecheck: run mypy over src and tests
typecheck:
	$(PYTHON) -m mypy

## fmt: auto-format and auto-fix lint findings
fmt:
	$(PYTHON) -m ruff format src tests
	$(PYTHON) -m ruff check --fix src tests

## check: everything CI would run
check: lint typecheck test
